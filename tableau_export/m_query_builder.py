"""
Power Query M Query Builder — Generates M queries from Tableau connections.

Extracted from datasource_extractor.py for maintainability.
Each connector type has its own generator function dispatched via _M_GENERATORS.
"""

import logging
import ntpath
import re

logger = logging.getLogger(__name__)

# ── Type mapping ──────────────────────────────────────────────────────────────

_M_TYPE_MAP = {
    'integer': 'Int64.Type',
    'int64': 'Int64.Type',
    'real': 'type number',
    'double': 'type number',
    'decimal': 'type number',
    'number': 'type number',
    'string': 'type text',
    'boolean': 'type logical',
    'date': 'type date',
    'datetime': 'type datetime',
    'time': 'type time',
    'spatial': 'type text',
    'binary': 'type binary',
    'currency': 'Currency.Type',
    'percentage': 'Percentage.Type',
}


def map_tableau_to_m_type(datatype):
    """Maps Tableau/BIM types to Power Query M types."""
    return _M_TYPE_MAP.get((datatype or '').lower(), 'type text')


def _file_basename(path):
    """Extract just the filename from a potentially full file path.

    DataFolder already contains the directory, so M expressions should only
    reference the filename (e.g. ``DataFolder & "\\file.xlsx"``), not repeat
    the full directory path.
    """
    # Normalise separators, then take the last component
    name = path.replace('\\', '/').rsplit('/', 1)[-1]
    return name if name else path


def _m_escape_col_name(name):
    """Escape column names for M queries (double-quote any internal quotes)."""
    return name.replace('"', '""')


def _unquote_tableau_file_identifier(raw):
    """Remove one SQL identifier wrapper, preserving literal apostrophes."""
    name = (raw or '').strip()
    if len(name) >= 2:
        if name[0] == '[' and name[-1] == ']':
            return name[1:-1].replace(']]', ']')
        if name[0] in ('"', "'") and name[-1] == name[0]:
            return name[1:-1].replace(name[0] * 2, name[0])
    return name


def _clean_sheet_name(raw, fallback):
    """Normalise a Tableau-derived sheet/table name for Excel Item navigation.

    Tableau often stores the source table as a qualified identifier such as
    ``[extract].[Extract]`` or ``[Sheet1$]``.  When embedded directly into an
    M ``Item="..."`` clause the brackets and ``].[`` separator leak into the
    workbook navigation and break the query.  This takes the last segment,
    removes SQL identifier quoting and one trailing sheet-marker ``$``.
    Apostrophes inside the name are data, not quoting to strip.
    """
    name = (raw or '').strip()
    if not name:
        # A fallback is a model name, not SQL syntax: its quotes are literal.
        return _m_escape_col_name((fallback or '').removesuffix('$'))
    # Take the last qualified segment: ``[ds].[Extract]`` → ``Extract]``
    # Also accept ``ds].[Extract`` from older extraction metadata.
    if '].[' in name:
        name = name.rsplit('].[', 1)[-1].removesuffix(']')
    else:
        name = _unquote_tableau_file_identifier(name)
    name = name.removesuffix('$')
    if not name:
        name = (fallback or '').removesuffix('$')
    return _m_escape_col_name(name)


def _sql_source_identity(details, table_name, default_schema):
    """Resolve a physical SQL relation independently of its model caption."""
    relation = (details.get('_source_table') or '').strip()
    schema = details.get('schema') or default_schema
    if '].[' in relation:
        parts = relation.split('].[')
        schema = parts[-2].lstrip('[').replace(']]', ']')
        item = parts[-1].rstrip(']').replace(']]', ']')
    elif '"."' in relation:
        parts = relation.split('"."')
        schema = parts[-2].lstrip('"').replace('""', '"')
        item = parts[-1].rstrip('"').replace('""', '"')
    else:
        item = _unquote_tableau_file_identifier(relation) if relation else table_name
    return schema, item


def _m_escape_string(value):
    """Escape a value for embedding inside an M double-quoted string literal.

    Doubles any internal double-quote characters so that ``"server""name"``
    is produced for a value containing a literal quote.
    """
    return (value or '').replace('"', '""')


def _m_literal(value):
    """Return *value* as an escaped M text literal."""
    return '"' + _m_escape_string(str(value)) + '"'


def _m_header_literal(name):
    """Quote an exact source header without interpreting M escape sequences."""
    escaped = _m_escape_col_name(name).replace('#(', '#(#)(')
    escaped = escaped.replace('\r', '#(cr)').replace('\n', '#(lf)').replace('\t', '#(tab)')
    return '"' + escaped + '"'


def _m_field_ref(name):
    """Return a quoted M field reference for arbitrary Tableau metadata."""
    if name.startswith('[') and name.endswith(']'):
        name = name[1:-1]
    if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
        return '[' + name + ']'
    return '[#' + _m_literal(name) + ']'


def _odbc_escape(value):
    """Sanitise a value for embedding inside an ODBC connection string.

    Strips semicolons to prevent DSN parameter injection.
    Also escapes double-quotes for M string safety.
    """
    return _m_escape_string((value or '').replace(';', ''))


# ── Column type change step (shared helper) ──────────────────────────────────

def _build_type_changes(columns):
    """Build a list of M type-change entries for a set of columns."""
    entries = []
    for col in columns:
        m_type = map_tableau_to_m_type(col['datatype'])
        col_name = _m_header_literal(col['name'])
        entries.append('{' + col_name + ', ' + m_type + '}')
    return entries


def _promote_headers_with_schema(table_ref, columns):
    """Promote headers and reconcile only missing declared source names.

    Exact ordinal matches reserve their raw headers, including intentional
    whitespace. Only otherwise-unclaimed headers may match an unchanged
    expected name through M's default Unicode-aware Text.Trim. Ambiguous or
    missing matches fail at refresh without disclosing row or endpoint values.
    No schema means ordinary promotion, with no inferred renames.
    """
    promotion = f'Table.PromoteHeaders({table_ref}, [PromoteAllScalars=true])'
    if not columns:
        return promotion
    if any(not isinstance(col.get('name'), str) for col in columns):
        raise ValueError("Declared source header names must be text.")
    expected = ', '.join(_m_header_literal(col['name']) for col in columns)
    return f'''let
        Expected = {{{expected}}},
        BlankExpectedNames = List.Select(Expected, each Text.Trim(_) = ""),
        DuplicateExpectedNames = List.Select(List.Distinct(Expected, Comparer.Ordinal),
            (name) => List.Count(List.Select(Expected, (other) => Comparer.Ordinal(name, other) = 0)) > 1),
        Promoted = {promotion},
        Headers = Table.ColumnNames(Promoted),
        Exact = List.Select(Expected, (name) => List.Contains(Headers, name, Comparer.Ordinal)),
        Available = List.Select(Headers, (name) => not List.Contains(Exact, name, Comparer.Ordinal)),
        Missing = List.Select(Expected, (name) => not List.Contains(Exact, name, Comparer.Ordinal)),
        Matches = List.Transform(Missing, (name) => [
            ExpectedName = name,
            Candidates = List.Select(Available, (header) => Comparer.Ordinal(Text.Trim(header), name) = 0)
        ]),
        MissingExpectedNames = List.Transform(
            List.Select(Matches, each List.Count([Candidates]) = 0), each [ExpectedName]),
        AmbiguousExpectedNames = List.Transform(
            List.Select(Matches, each List.Count([Candidates]) > 1), each [ExpectedName]),
        Renames = List.Transform(Matches, each {{[Candidates]{{0}}, [ExpectedName]}}),
        RenameSources = List.Transform(Renames, each _{{0}})
    in
        if not List.IsEmpty(BlankExpectedNames) or not List.IsEmpty(DuplicateExpectedNames) then
            error Error.Record("HeaderSchema.Error",
                "Declared source column names must be nonblank and unique.",
                [BlankExpectedNames = BlankExpectedNames, DuplicateExpectedNames = DuplicateExpectedNames])
        else if not List.IsEmpty(MissingExpectedNames) or not List.IsEmpty(AmbiguousExpectedNames) then
            error Error.Record("HeaderMatch.Error",
                "Source headers must uniquely match the declared source column names.",
                [MissingExpectedNames = MissingExpectedNames, AmbiguousExpectedNames = AmbiguousExpectedNames])
        else if List.Count(List.Distinct(RenameSources, Comparer.Ordinal)) <> List.Count(Renames) then
            error Error.Record("HeaderMatch.Error",
                "Source header matches must be one-to-one.",
                [ExpectedNames = Missing])
        else Table.RenameColumns(Promoted, Renames)'''


def _append_type_step(m_query, columns, prev_step='#"Promoted Headers"'):
    """Append a #"Changed Types" step to an M query."""
    type_changes = _build_type_changes(columns)
    if type_changes:
        m_query += f'    #"Changed Types" = Table.TransformColumnTypes({prev_step}, {{\n        '
        m_query += ',\n        '.join(type_changes)
        m_query += '\n    }),\n'
        m_query += '    Result = #"Changed Types"\n'
    else:
        m_query += f'    Result = {prev_step}\n'
    m_query += 'in\n    Result'
    return m_query


# ── Per-connector generators ─────────────────────────────────────────────────

def _gen_m_source_error(table_name, message):
    """Emit an explicit refresh error rather than a fabricated file binding."""
    logger.warning("Source binding unavailable for table %r: %s", table_name, message)
    return (
        'let\n'
        f'    Source = error Error.Record("SourceBinding.Error", {_m_literal(message)}, '
        f'[Table={_m_literal(table_name)}])\n'
        'in\n    Source'
    )


def _is_windows_drive_absolute_path(path):
    return bool(re.match(r'^[A-Za-z]:[\\/]', path))


def _is_windows_drive_relative_path(path):
    return bool(re.match(r'^[A-Za-z]:', path)) and not _is_windows_drive_absolute_path(path)


def _is_unc_path(path):
    return path.startswith(('\\\\', '//'))


def _is_posix_absolute_path(path):
    return path.startswith('/') and not path.startswith('//')


def _is_rooted_or_ambiguous_path(path):
    return (
        path.startswith(('\\', '/'))
        or _is_windows_drive_absolute_path(path)
        or _is_windows_drive_relative_path(path)
    )


def _is_absolute_source_base(path):
    return (
        _is_windows_drive_absolute_path(path)
        or _is_unc_path(path)
        or _is_posix_absolute_path(path)
    )


def _join_native_source_path(base_dir, relative_path):
    """Join Tableau's relative path syntax to an absolute native source base."""
    windows_path = (
        _is_windows_drive_absolute_path(base_dir)
        or _is_unc_path(base_dir)
    )
    if windows_path:
        separator = '\\'
        base_dir = base_dir.replace('/', '\\')
        relative_path = relative_path.replace('/', '\\')
    else:
        separator = '/'
        relative_path = relative_path.replace('\\', '/')

    if base_dir.endswith(separator):
        return base_dir + relative_path
    return base_dir + separator + relative_path


def _file_contents(table_name, filename, directory='', native=False, *, source_base_dir=None):
    """Build a staged or source-relative file binding from metadata, without file I/O."""
    if not native:
        name = _file_basename(filename)
        return name, ('File.Contents(DataFolder & "\\'
                      + _m_escape_string(name.replace('/', '\\')) + '")')

    path = str(filename)
    directory = str(directory or '')
    drive_relative = _is_windows_drive_relative_path(path)
    rooted = path.startswith(('\\', '/')) or _is_windows_drive_absolute_path(path)
    if drive_relative:
        # C:relative is anchored to the current directory on drive C.  Neither
        # a declared directory nor the workbook base can resolve that safely.
        directory = ''
    elif directory and not rooted:
        if re.fullmatch(r'[A-Za-z]:', directory):
            # Keep a bare drive prefix drive-relative (C:file, not C:\file).
            path = directory + path
        else:
            separator = '/' if directory.startswith('/') and not directory.startswith('//') else '\\'
            path = directory + ('' if directory[-1] in '\\/' else separator) + path

    source_base = str(source_base_dir or '')
    if (_is_absolute_source_base(source_base)
            and not _is_rooted_or_ambiguous_path(path)):
        path = _join_native_source_path(source_base, path)

    if not (path.startswith('/') and not path.startswith('//')):
        path = path.replace('/', '\\')
    label = ' '.join(path.splitlines()) or path
    absolute_windows_path = _is_windows_drive_absolute_path(path) or _is_unc_path(path)
    absolute_posix_path = (
        _is_posix_absolute_path(source_base)
        and _is_posix_absolute_path(path)
    )
    if not (absolute_windows_path or absolute_posix_path):
        logger.warning(
            "Native file source for table %r keeps its declared location %r, "
            "which is not an absolute Windows path; configure the original "
            "location before refresh. The binding is not rebased onto DataFolder.",
            table_name, path,
        )
        label += ' (not an absolute path: configure the original location before refresh)'
    # M treats '#(' as an escape introducer, but backslashes are literal.
    escaped = _m_escape_string(path).replace('#(', '#(#)(')
    escaped = escaped.replace('\r', '#(cr)').replace('\n', '#(lf)').replace('\t', '#(tab)')
    return label, 'File.Contents("' + escaped + '")'


def _gen_m_excel(details, table_name, columns, *, use_original_file_paths=False):
    if not details.get('filename'):
        return _gen_m_source_error(table_name, 'Excel source filename is missing; supply an explicit workbook binding.')
    label, contents = _file_contents(
        table_name, details['filename'], details.get('directory'), use_original_file_paths,
        source_base_dir=(
            details.get('_source_base_dir') if use_original_file_paths else None))
    sheet_name = _clean_sheet_name(details.get('_source_table', ''), table_name)
    safe_step = '#"' + _m_escape_col_name(table_name) + ' Sheet"'
    if not details.get('_source_table'):
        logger.warning(
            "Excel worksheet binding for table %r has no source_table metadata; "
            "using its exact name %r, unverified until refresh (no alternate sheet substitution).",
            table_name, sheet_name,
        )

    m_query = 'let\n'
    m_query += f'    // Source Excel: {label}\n'
    m_query += f'    Source = Excel.Workbook({contents}, null, true),\n'
    m_query += f'    {safe_step} = Source{{[Item="{sheet_name}",Kind="Sheet"]}}[Data],\n'
    m_query += f'    #"Promoted Headers" = {_promote_headers_with_schema(safe_step, columns)},\n'
    return _append_type_step(m_query, columns)


def _gen_m_schema_item(details, table_name, columns,
                      comment, pq_func, server_arg, db_arg, schema='dbo'):
    """Generic M generator for connectors using Schema+Item navigation."""
    safe = '#"' + _m_escape_col_name(table_name) + ' Table"'
    srv = _m_escape_string(server_arg)
    db = _m_escape_string(db_arg)
    schema, item = _sql_source_identity(details, table_name, schema)
    sch = _m_escape_string(schema)
    tbl = _m_escape_string(item)
    m_query = 'let\n'
    m_query += f'    // Source {comment}\n'
    m_query += f'    Source = {pq_func}("{srv}", "{db}"),\n'
    m_query += f'    {safe} = Source{{[Schema="{sch}", Item="{tbl}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_sql_server(details, table_name, columns):
    server = details.get('server', 'localhost')
    database = details.get('database', 'MyDatabase')
    return _gen_m_schema_item(details, table_name, columns,
                              'SQL Server', 'Sql.Database', server, database, 'dbo')


def _gen_m_postgresql(details, table_name, columns):
    server = details.get('server', 'localhost')
    port = details.get('port', '5432')
    database = details.get('database', 'postgres')
    return _gen_m_schema_item(details, table_name, columns,
                              'PostgreSQL', 'PostgreSQL.Database',
                              f'{server}:{port}', database, 'public')


def _gen_m_csv(details, table_name, columns, *, use_original_file_paths=False):
    # A physical text relation is authoritative even when Tableau reuses a
    # named connection whose filename belongs to another relation. Tableau's
    # text driver represents the extension separator as '#' in SQL identifiers.
    relation_file = _unquote_tableau_file_identifier(details.get('_source_table', ''))
    relation_file = re.sub(r'#(csv|tsv|txt|tab)$', r'.\1', relation_file, flags=re.IGNORECASE)
    directory = details.get('directory')
    if re.search(r'\.(csv|tsv|txt|tab)$', relation_file, re.IGNORECASE):
        filename = relation_file
        if use_original_file_paths and not directory:
            directory = ntpath.dirname(details.get('filename') or '')
    elif details.get('filename'):
        filename = details['filename']
        if relation_file:
            logger.warning(
                "CSV table %r has no filename in its source relation %r; "
                "connection filename %r is unverified for this relation.",
                table_name, relation_file, _file_basename(filename),
            )
    else:
        return _gen_m_source_error(table_name, 'CSV source filename is missing; supply an explicit file binding.')
    delimiter = details.get('delimiter', ',')
    encoding = details.get('encoding', 'utf-8').upper()
    encoding_code = {'UTF-8': '65001', 'UTF8': '65001'}.get(encoding, '65001')
    label, contents = _file_contents(
        table_name, filename, directory, use_original_file_paths,
        source_base_dir=(
            details.get('_source_base_dir') if use_original_file_paths else None))
    # A native model can select only part of the physical CSV schema.
    columns_option = (
        f'        Columns={len(columns)},\n'
        if columns and not use_original_file_paths else ''
    )

    m_query = f'''let
    // Source CSV: {label}
    Source = Csv.Document({contents}, [
        Delimiter="{_m_escape_string(delimiter)}",
{columns_option}        Encoding={encoding_code},
        QuoteStyle=QuoteStyle.None
    ]),
    #"Promoted Headers" = {_promote_headers_with_schema("Source", columns)},
'''
    if columns:
        prev_step = '#"Promoted Headers"'
        if use_original_file_paths:
            # Reconcile headers before selection; captions are still applied later.
            names = ', '.join(_m_header_literal(col['name']) for col in columns)
            m_query += f'    #"Selected Columns" = Table.SelectColumns({prev_step}, {{{names}}}),\n'
            prev_step = '#"Selected Columns"'
        return _append_type_step(m_query, columns, prev_step=prev_step)
    else:
        m_query += '    Result = #"Promoted Headers"\nin\n    Result'
        return m_query


def _gen_m_bigquery(details, table_name, columns):
    project = details.get('project', 'my-project')
    dataset = details.get('dataset', 'my_dataset')
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Google BigQuery: {project}.{dataset}\n'
    m_query += f'    Source = GoogleBigQuery.Database([BillingProject="{project}"]),\n'
    m_query += f'    #"{dataset}" = Source{{[Name="{dataset}"]}}[Data],\n'
    m_query += f'    {safe} = #"{dataset}"{{[Name="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_mysql(details, table_name, columns):
    server = details.get('server', 'localhost')
    port = details.get('port', '3306')
    database = details.get('database', 'mydb')
    return _gen_m_schema_item(details, table_name, columns,
                              f'MySQL: {server}:{port}', 'MySQL.Database',
                              f'{server}:{port}', database, database)


def _gen_m_oracle(details, table_name, columns):
    server = _m_escape_string(details.get('server', 'localhost'))
    service = _m_escape_string(details.get('service', 'ORCL'))
    port = details.get('port', '1521')
    schema, item = _sql_source_identity(details, table_name, details.get('schema') or 'DBO')
    tbl = _m_escape_string(item)
    safe = '#"' + _m_escape_col_name(table_name) + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Oracle: {server}:{port}/{service}\n'
    m_query += f'    Source = Oracle.Database("{server}:{port}/{service}"),\n'
    m_query += f'    {safe} = Source{{[Schema="{_m_escape_string(schema)}", Item="{tbl}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_snowflake(details, table_name, columns):
    server = _m_escape_string(details.get('server', 'account.snowflakecomputing.com'))
    database = _m_escape_string(details.get('database', 'MY_DB'))
    warehouse = _m_escape_string(details.get('warehouse', 'MY_WH'))
    schema = _m_escape_string(details.get('schema', 'PUBLIC'))
    tbl = _m_escape_string(table_name)
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Snowflake: {server}\n'
    m_query += f'    Source = Snowflake.Databases("{server}", "{warehouse}"),\n'
    m_query += f'    #"{database}" = Source{{[Name="{database}"]}}[Data],\n'
    m_query += f'    #"{schema}" = #"{database}"{{[Name="{schema}"]}}[Data],\n'
    m_query += f'    {safe} = #"{schema}"{{[Name="{tbl}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_geojson(details, table_name, columns, *, use_original_file_paths=False):
    if use_original_file_paths and not details.get('filename'):
        return _gen_m_source_error(
            table_name, 'GeoJSON source filename is missing; supply an explicit file binding.')
    if (use_original_file_paths
            and ntpath.splitext(str(details.get('filename', '')))[1].lower() == '.shp'):
        return _gen_m_source_error(
            table_name,
            'Native shapefile is not GeoJSON. Configure a supported GeoJSON '
            'source in Power Query; native spatial data are not converted or '
            'copied during PBIP migration.',
        )
    label, contents = _file_contents(
        table_name, details.get('filename', 'file.geojson'),
        details.get('directory'), use_original_file_paths,
        source_base_dir=(
            details.get('_source_base_dir') if use_original_file_paths else None))

    prop_cols = [col for col in columns if col.get('name', '') != 'Geometry']
    prop_names = ", ".join([f'"{_m_escape_col_name(col["name"])}"' for col in prop_cols])

    type_changes = []
    for col in columns:
        cname = col.get('name', '')
        if cname.lower() == 'geometry':
            continue
        m_type = map_tableau_to_m_type(col.get('datatype', 'string'))
        type_changes.append(f'{{"{_m_escape_col_name(cname)}", {m_type}}}')
    type_step = ',\n        '.join(type_changes)

    has_geometry = any(col.get('name', '').lower() == 'geometry' for col in columns)

    m_query = f'''let
    // Source GeoJSON: {label}
    Source = Json.Document({contents}),
    features = Source[features],
    #"Converted to Table" = Table.FromList(features, Splitter.SplitByNothing(), null, null, ExtraValues.Error),
    #"Expanded Column1" = Table.ExpandRecordColumn(#"Converted to Table", "Column1", {{"properties", "geometry"}}),
    #"Expanded properties" = Table.ExpandRecordColumn(#"Expanded Column1", "properties", {{{prop_names}}}),'''

    if has_geometry:
        m_query += '''
    #"Geometry to Text" = Table.TransformColumns(#"Expanded properties", {{"geometry", each Text.FromBinary(Json.FromValue(_)), type text}}),
    #"Renamed Geometry" = Table.RenameColumns(#"Geometry to Text", {{"geometry", "Geometry"}}),'''
        last_step = '#"Renamed Geometry"'
    else:
        last_step = '#"Expanded properties"'

    if type_changes:
        m_query += f'''
    #"Changed Types" = Table.TransformColumnTypes({last_step}, {{
        {type_step}
    }})
in
    #"Changed Types"'''
    else:
        m_query += f'''
in
    {last_step}'''

    return m_query


def _gen_m_fallback(details, table_name, columns):
    conn_type = details.get('_conn_type', 'Unknown')
    named_cols = [col for col in columns if 'name' in col]
    col_list = ", ".join([f'"{_m_escape_col_name(col["name"])}"' for col in named_cols])
    return f'''let
    // TODO: Configure the data source for connector type: {conn_type}
    Source = #table({{{col_list}}}, {{}})
in
    Source'''


# ── Additional connectors ────────────────────────────────────────────────────

def _gen_m_teradata(details, table_name, columns):
    server = details.get('server', 'localhost')
    database = details.get('database', 'MyDB')
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Teradata: {server}\n'
    m_query += f'    Source = Teradata.Database("{server}"),\n'
    m_query += f'    #"{database}" = Source{{[Name="{database}"]}}[Data],\n'
    m_query += f'    {safe} = #"{database}"{{[Name="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_sap_hana(details, table_name, columns):
    server = details.get('server', 'localhost')
    port = details.get('port', '30015')
    schema = _m_escape_string(details.get('schema', 'PUBLIC'))
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source SAP HANA: {server}:{port}\n'
    m_query += f'    Source = SapHana.Database("{server}:{port}"),\n'
    m_query += f'    {safe} = Source{{[Schema="{schema}", Name="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_sap_bw(details, table_name, columns):
    server = details.get('server', 'sap-bw-server')
    system_number = details.get('system_number', '00')
    client_id = details.get('client_id', '')
    language = details.get('language', 'EN')
    cube = details.get('cube', table_name)
    catalog = details.get('catalog', '$INFOCUBE')

    m_query = 'let\n'
    m_query += f'    // Source SAP BW: {server} (System {system_number})\n'
    m_query += f'    Source = SapBusinessWarehouse.Cubes("{server}", "{system_number}", "{client_id}", [Language="{language}"]),\n'
    m_query += f'    #"{catalog}" = Source{{[Name="{catalog}"]}}[Data],\n'
    m_query += f'    #"{cube}" = #"{catalog}"{{[Name="{cube}"]}}[Data],\n'
    m_query += f'    Result = #"{cube}"\nin\n    Result'
    return m_query


def _gen_m_redshift(details, table_name, columns):
    server = details.get('server', 'cluster.redshift.amazonaws.com')
    port = details.get('port', '5439')
    database = details.get('database', 'mydb')
    return _gen_m_schema_item(details, table_name, columns,
                              f'Amazon Redshift: {server}:{port}',
                              'AmazonRedshift.Database',
                              f'{server}:{port}', database, 'public')


def _gen_m_databricks(details, table_name, columns):
    server = details.get('server', 'adb-xxxxx.azuredatabricks.net')
    http_path = details.get('http_path', '/sql/1.0/warehouses/xxxxx')
    catalog = details.get('catalog', 'main')
    schema = details.get('schema', 'default')
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Databricks: {server}\n'
    m_query += f'    Source = Databricks.Catalogs("{server}", "{http_path}"),\n'
    m_query += f'    #"{catalog}" = Source{{[Name="{catalog}"]}}[Data],\n'
    m_query += f'    #"{schema}" = #"{catalog}"{{[Name="{schema}"]}}[Data],\n'
    m_query += f'    {safe} = #"{schema}"{{[Name="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_spark(details, table_name, columns):
    server = details.get('server', 'localhost')
    port = details.get('port', '10000')
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Spark SQL: {server}:{port}\n'
    m_query += f'    Source = SparkSql.Database("{server}", "{port}"),\n'
    m_query += f'    {safe} = Source{{[Name="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_azure_sql(details, table_name, columns):
    server = details.get('server', 'myserver.database.windows.net')
    database = details.get('database', 'MyDatabase')
    return _gen_m_schema_item(details, table_name, columns,
                              'Azure SQL Database', 'AzureSQL.Database', server, database, 'dbo')


def _gen_m_synapse(details, table_name, columns):
    server = details.get('server', 'myworkspace.sql.azuresynapse.net')
    database = details.get('database', 'MyPool')
    return _gen_m_schema_item(details, table_name, columns,
                              'Azure Synapse Analytics', 'AzureSQL.Database', server, database, 'dbo')


def _gen_m_google_sheets(details, table_name, columns):
    spreadsheet_id = details.get('spreadsheet_id', 'SPREADSHEET_ID')
    sheet_name = details.get('sheet_name', table_name)

    m_query = 'let\n'
    m_query += f'    // Source Google Sheets: {spreadsheet_id}\n'
    m_query += f'    Source = Web.Contents("https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?tqx=out:csv&sheet={sheet_name}"),\n'
    m_query += '    Parsed = Csv.Document(Source, [Delimiter=",", Encoding=65001]),\n'
    m_query += f'    #"Promoted Headers" = {_promote_headers_with_schema("Parsed", columns)},\n'
    return _append_type_step(m_query, columns)


def _gen_m_sharepoint(details, table_name, columns):
    site_url = details.get('site_url', 'https://contoso.sharepoint.com/sites/mysite')
    filename = details.get('filename', table_name + '.xlsx')
    sheet_name = _clean_sheet_name(details.get('_source_table', ''), table_name)

    m_query = 'let\n'
    m_query += f'    // Source SharePoint: {site_url}\n'
    m_query += f'    Source = SharePoint.Files("{site_url}", [ApiVersion = 15]),\n'
    m_query += f'    FileRow = Table.SelectRows(Source, each [Name] = "{filename}"),\n'
    m_query += '    FileContent = FileRow{{0}}[Content],\n'
    m_query += '    Workbook = Excel.Workbook(FileContent, null, true),\n'
    m_query += f'    Sheet = Workbook{{[Item="{sheet_name}",Kind="Sheet"]}}[Data],\n'
    m_query += f'    #"Promoted Headers" = {_promote_headers_with_schema("Sheet", columns)},\n'
    return _append_type_step(m_query, columns)


def _gen_m_json(details, table_name, columns, *, use_original_file_paths=False):
    if use_original_file_paths and not details.get('filename'):
        return _gen_m_source_error(
            table_name, 'JSON source filename is missing; supply an explicit file binding.')
    label, contents = _file_contents(
        table_name, details.get('filename', table_name + '.json'),
        details.get('directory'), use_original_file_paths,
        source_base_dir=(
            details.get('_source_base_dir') if use_original_file_paths else None))

    m_query = 'let\n'
    m_query += f'    // Source JSON: {label}\n'
    m_query += f'    Source = Json.Document({contents}),\n'
    m_query += '    #"Converted to Table" = if Value.Is(Source, type list) then Table.FromRecords(Source) else Table.FromRecords({Source}),\n'
    m_query += '    #"Promoted Headers" = #"Converted to Table",\n'
    return _append_type_step(m_query, columns)


def _gen_m_xml(details, table_name, columns, *, use_original_file_paths=False):
    if use_original_file_paths and not details.get('filename'):
        return _gen_m_source_error(
            table_name, 'XML source filename is missing; supply an explicit file binding.')
    label, contents = _file_contents(
        table_name, details.get('filename', table_name + '.xml'),
        details.get('directory'), use_original_file_paths,
        source_base_dir=(
            details.get('_source_base_dir') if use_original_file_paths else None))

    m_query = 'let\n'
    m_query += f'    // Source XML: {label}\n'
    m_query += f'    Source = Xml.Tables({contents}),\n'
    m_query += '    #"Promoted Headers" = Source,\n'
    return _append_type_step(m_query, columns)


def _gen_m_pdf(details, table_name, columns, *, use_original_file_paths=False):
    if use_original_file_paths and not details.get('filename'):
        return _gen_m_source_error(
            table_name, 'PDF source filename is missing; supply an explicit file binding.')
    label, contents = _file_contents(
        table_name, details.get('filename', table_name + '.pdf'),
        details.get('directory'), use_original_file_paths,
        source_base_dir=(
            details.get('_source_base_dir') if use_original_file_paths else None))

    # PDF connector depth: page range and table selection
    start_page = details.get('start_page')
    end_page = details.get('end_page')
    table_index = details.get('table_index', 0)

    options_parts = []
    if start_page is not None:
        options_parts.append(f'StartPage={int(start_page)}')
    if end_page is not None:
        options_parts.append(f'EndPage={int(end_page)}')
    options_str = '[' + ', '.join(options_parts) + ']' if options_parts else ''

    m_query = 'let\n'
    m_query += f'    // Source PDF: {label}\n'
    if options_str:
        m_query += f'    Source = Pdf.Tables({contents}, {options_str}),\n'
    else:
        m_query += f'    Source = Pdf.Tables({contents}),\n'
    m_query += f'    Table1 = Source{{{{{table_index}}}}}[Data],\n'
    m_query += f'    #"Promoted Headers" = {_promote_headers_with_schema("Table1", columns)},\n'
    return _append_type_step(m_query, columns)


def _gen_m_salesforce(details, table_name, columns):
    safe = '#"' + table_name + ' Table"'
    soql = details.get('soql', '')
    api_version = details.get('api_version', '')

    m_query = 'let\n'
    m_query += '    // Source Salesforce\n'

    # Build Salesforce.Data options
    sf_options = []
    if api_version:
        sf_options.append(f'[ApiVersion="{api_version}"]')

    if soql:
        # SOQL passthrough query
        if sf_options:
            m_query += f'    Source = Salesforce.Data(null, {sf_options[0]}),\n'
        else:
            m_query += '    Source = Salesforce.Data(),\n'
        m_query += f'    {safe} = Value.NativeQuery(Source, "{soql}"),\n'
    else:
        # Standard table navigation
        if sf_options:
            m_query += f'    Source = Salesforce.Data(null, {sf_options[0]}),\n'
        else:
            m_query += '    Source = Salesforce.Data(),\n'
        m_query += f'    {safe} = Source{{[Name="{table_name}"]}}[Data],\n'

    # Relationship traversal: expand lookup columns
    relationships = details.get('relationships', [])
    prev_step = safe
    for i, rel in enumerate(relationships):
        rel_col = rel.get('column', '')
        expand_cols = rel.get('expand', [])
        if rel_col and expand_cols:
            expand_list = ', '.join([f'"{c}"' for c in expand_cols])
            step_name = f'#"Expanded {rel_col}"'
            m_query += (f'    {step_name} = Table.ExpandRecordColumn({prev_step}, '
                        f'"{rel_col}", {{{expand_list}}}),\n')
            prev_step = step_name

    m_query += f'    Result = {prev_step}\nin\n    Result'
    return m_query


def _gen_m_web(details, table_name, columns):
    url = details.get('url', 'https://api.example.com/data')

    m_query = 'let\n'
    m_query += f'    // Source Web: {url}\n'
    m_query += f'    Source = Web.Contents("{url}"),\n'
    m_query += '    Json = Json.Document(Source),\n'
    m_query += '    #"Converted to Table" = Table.FromRecords(if Value.Is(Json, type list) then Json else {Json}),\n'
    m_query += '    #"Promoted Headers" = #"Converted to Table",\n'
    return _append_type_step(m_query, columns)


def _gen_m_custom_sql(details, table_name, columns):
    """Generate M query using native SQL query (for Tableau custom SQL sources).

    Supports parameter binding via Value.NativeQuery's optional record argument.
    Parameters are extracted from the ``params`` key in *details*.
    """
    server = _m_escape_string(details.get('server', 'localhost'))
    database = _m_escape_string(details.get('database', 'MyDatabase'))
    sql_query = details.get('sql_query', f'SELECT * FROM {table_name}')
    params = details.get('params', {})  # {name: default_value}
    # Escape quotes in SQL for M string
    sql_escaped = sql_query.replace('"', '""')

    m_query = 'let\n'
    m_query += '    // Source: Custom SQL Query\n'
    if params:
        # Build parameter record:  [Param1="value1", Param2="value2"]
        param_items = ', '.join(f'{k}="{str(v).replace(chr(34), chr(34)+chr(34))}"' for k, v in params.items())
        m_query += f'    Source = Value.NativeQuery(Sql.Database("{server}", "{database}"), "'
        m_query += sql_escaped
        m_query += f'", [{param_items}], [EnableFolding=true]),\n'
    else:
        m_query += f'    Source = Sql.Database("{server}", "{database}", [Query="'
        m_query += sql_escaped
        m_query += '"]),\n'
    m_query += '    Result = Source\nin\n    Result'
    return m_query


def _gen_m_odata(details, table_name, columns):
    """Generate M query for OData feed."""
    url = details.get('server', details.get('url', 'https://services.odata.org/V4/Northwind/Northwind.svc'))
    m_query = 'let\n'
    m_query += f'    // Source OData: {url}\n'
    m_query += f'    Source = OData.Feed("{url}"),\n'
    m_query += f'    {table_name}_Table = Source{{[Name="{table_name}",Signature="table"]}}[Data],\n'
    m_query += f'    #"Promoted Headers" = {table_name}_Table,\n'
    return _append_type_step(m_query, columns)


def _gen_m_google_analytics(details, table_name, columns):
    """Generate M query for Google Analytics."""
    view_id = details.get('view_id', details.get('server', 'GA_VIEW_ID'))
    m_query = 'let\n'
    m_query += f'    // Source Google Analytics: View {view_id}\n'
    m_query += '    // Note: Requires Google Analytics connector in Power BI Desktop\n'
    m_query += '    Source = GoogleAnalytics.Accounts(),\n'
    m_query += f'    ViewData = Source{{[Name="{view_id}"]}}[Data],\n'
    m_query += '    #"Promoted Headers" = ViewData,\n'
    return _append_type_step(m_query, columns)


def _gen_m_azure_blob(details, table_name, columns):
    """Generate M query for Azure Blob Storage / ADLS Gen2."""
    account = details.get('server', details.get('account', 'mystorageaccount'))
    container = details.get('database', details.get('container', 'mycontainer'))
    # Detect ADLS Gen2 vs Blob by URL pattern
    is_adls = 'dfs.core.windows.net' in account or 'adls' in account.lower()
    if is_adls:
        m_query = 'let\n'
        m_query += f'    // Source Azure Data Lake Storage Gen2: {account}\n'
        m_query += f'    Source = AzureStorage.DataLake("https://{account}.dfs.core.windows.net/{container}"),\n'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Azure Blob Storage: {account}\n'
        m_query += f'    Source = AzureStorage.Blobs("https://{account}.blob.core.windows.net/{container}"),\n'
    m_query += f'    FileRow = Table.SelectRows(Source, each Text.Contains([Name], "{table_name}")),\n'
    m_query += '    FileContent = FileRow{{0}}[Content],\n'
    m_query += '    Parsed = Csv.Document(FileContent, [Delimiter=",", Encoding=65001]),\n'
    m_query += f'    #"Promoted Headers" = {_promote_headers_with_schema("Parsed", columns)},\n'
    return _append_type_step(m_query, columns)


def _gen_m_vertica(details, table_name, columns):
    """Generate M query for Vertica (via ODBC)."""
    server = _odbc_escape(details.get('server', 'vertica-server'))
    database = _odbc_escape(details.get('database', 'MyDatabase'))
    schema = _m_escape_string(details.get('schema', 'public'))
    m_query = 'let\n'
    m_query += f'    // Source Vertica: {server}/{database}\n'
    m_query += f'    Source = Odbc.DataSource("DSN=Vertica;Server={server};Database={database}"),\n'
    m_query += f'    SchemaTable = Source{{[Schema="{schema}",Item="{_m_escape_string(table_name)}"]}}[Data],\n'
    m_query += '    #"Promoted Headers" = SchemaTable,\n'
    return _append_type_step(m_query, columns)


def _gen_m_impala(details, table_name, columns):
    """Generate M query for Apache Impala."""
    server = _odbc_escape(details.get('server', 'impala-server'))
    port = _odbc_escape(details.get('port', '21050'))
    m_query = 'let\n'
    m_query += f'    // Source Impala: {server}:{port}\n'
    m_query += f'    Source = Odbc.DataSource("Driver={{Cloudera ODBC Driver for Impala}};Host={server};Port={port}"),\n'
    m_query += f'    Table = Source{{[Name="{_m_escape_string(table_name)}"]}}[Data],\n'
    m_query += '    #"Promoted Headers" = Table,\n'
    return _append_type_step(m_query, columns)


def _gen_m_hadoop_hive(details, table_name, columns):
    """Generate M query for Hadoop Hive / HDInsight."""
    server = _odbc_escape(details.get('server', 'hive-server'))
    port = _odbc_escape(details.get('port', '443'))
    m_query = 'let\n'
    m_query += f'    // Source Hadoop Hive: {server}:{port}\n'
    m_query += f'    Source = Odbc.DataSource("Driver={{Microsoft Hive ODBC Driver}};Host={server};Port={port}"),\n'
    m_query += f'    Table = Source{{[Name="{_m_escape_string(table_name)}"]}}[Data],\n'
    m_query += '    #"Promoted Headers" = Table,\n'
    return _append_type_step(m_query, columns)


def _gen_m_presto(details, table_name, columns):
    """Generate M query for Presto / Trino (via ODBC)."""
    server = _odbc_escape(details.get('server', 'presto-server'))
    catalog = _odbc_escape(details.get('database', details.get('catalog', 'hive')))
    schema = _odbc_escape(details.get('schema', 'default'))
    m_query = 'let\n'
    m_query += f'    // Source Presto/Trino: {server}/{catalog}.{schema}\n'
    m_query += f'    Source = Odbc.DataSource("Driver={{Starburst Presto ODBC Driver}};Host={server};Catalog={catalog};Schema={schema}"),\n'
    m_query += f'    Table = Source{{[Name="{_m_escape_string(table_name)}"]}}[Data],\n'
    m_query += '    #"Promoted Headers" = Table,\n'
    return _append_type_step(m_query, columns)


# ── Microsoft Fabric Lakehouse connector ─────────────────────────────────────

def _gen_m_fabric_lakehouse(details, table_name, columns):
    """Generate M query for Microsoft Fabric Lakehouse."""
    workspace_id = details.get('workspace_id', 'WORKSPACE_ID')
    lakehouse_id = details.get('lakehouse_id', 'LAKEHOUSE_ID')
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += '    // Source Microsoft Fabric Lakehouse\n'
    m_query += f'    Source = Lakehouse.Contents(null, "{workspace_id}", "{lakehouse_id}"),\n'
    m_query += f'    {safe} = Source{{[Id="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


def _gen_m_dataverse(details, table_name, columns):
    """Generate M query for Microsoft Dataverse (Common Data Service)."""
    org_url = details.get('server', details.get('org_url', 'https://org.crm.dynamics.com'))
    safe = '#"' + table_name + ' Table"'

    m_query = 'let\n'
    m_query += f'    // Source Dataverse: {org_url}\n'
    m_query += f'    Source = CommonDataService.Database("{org_url}"),\n'
    m_query += f'    {safe} = Source{{[Name="{table_name}"]}}[Data],\n'
    m_query += f'    Result = {safe}\nin\n    Result'
    return m_query


# ── Dispatch table ────────────────────────────────────────────────────────────

def _gen_m_hyper(details, table_name, columns):
    """Generate M query for a Hyper extract data source.

    Tries to load actual schema/data from hyper_reader; falls back to
    an inline #table() with the column list.
    """
    try:
        import os as _os

        from hyper_reader import generate_m_for_hyper_table, read_hyper
        filename = details.get('filename') or details.get('dbname', '')
        hyper_rows = details.get('hyper_max_rows', 20)
        if filename and _os.path.isfile(filename):
            result = read_hyper(filename, max_rows=hyper_rows)
            tables = result.get('tables', [])
            # Find matching table or use the first
            target = None
            for t in tables:
                if t.get('table', '').lower() == table_name.lower():
                    target = t
                    break
            if target is None and tables:
                target = tables[0]
            if target and target.get('columns'):
                return generate_m_for_hyper_table(target, row_limit=hyper_rows)
    except (ImportError, OSError, KeyError, ValueError) as exc:
        logger.debug('Hyper read failed for %s: %s', table_name, exc)

    # Fallback: structured #table() with column names from metadata
    col_list = ', '.join([f'"{_m_escape_col_name(col["name"])}"'
                          for col in columns if 'name' in col])
    return f'''let
    // Hyper extract: {table_name}
    // TODO: Replace with actual data source or imported CSV.
    Source = #table(
        {{{col_list}}},
        {{}}
    )
in
    Source'''


def _gen_m_sqlproxy(details, table_name, columns):
    """Generate M query for a Tableau Server Published Datasource (sqlproxy).

    sqlproxy is Tableau's internal connector for published datasources on
    Tableau Server/Cloud.  The actual data lives behind the published
    datasource — typically a database like SQL Server, Oracle, PostgreSQL, etc.

    The generated M query includes:
    - The Tableau Server URL and published datasource name as comments
    - A placeholder SQL Server connection (most common backend)
    - Alternative connection templates for Oracle, PostgreSQL, and Snowflake
    - A sample #table() fallback so the report opens without errors
    """
    server = details.get('server', 'tableau-server')
    ds_name = details.get('server_ds_name', '') or details.get('dbname', table_name)
    port = details.get('port', '443')
    channel = details.get('channel', 'https')

    col_list = ', '.join([f'"{_m_escape_col_name(col["name"])}"'
                          for col in columns if 'name' in col])

    return f'''let
    // ================================================================
    // Tableau Server Published Datasource: {ds_name}
    // Server: {channel}://{server}:{port}
    // ================================================================
    // This table was sourced from a Tableau Server published datasource.
    // Replace the empty source below with your actual database connection.
    //
    // Option A — SQL Server:
    //   Source = Sql.Database("your-server", "your-database"){{[Schema="dbo", Item="{table_name}"]}}[Data]
    //
    // Option B — Oracle:
    //   Source = Oracle.Database("your-server:1521/service"){{[Schema="SCHEMA", Name="{table_name.upper()}"]}}[Data]
    //
    // Option C — PostgreSQL:
    //   Source = PostgreSQL.Database("your-server:5432", "your-database"){{[Schema="public", Name="{table_name}"]}}[Data]
    //
    // Option D — Snowflake:
    //   Source = Snowflake.Databases("account.snowflakecomputing.com", "WAREHOUSE"){{[Name="DB"]}}[Data]{{[Schema="PUBLIC", Name="{table_name.upper()}"]}}[Data]
    // ================================================================
    Source = #table({{{col_list}}}, {{}})
in
    Source'''


# ── Sprint 61: New Connector Generators ───────────────────────────────────────

def _gen_m_mongodb(details, table_name, columns):
    """MongoDB Atlas / MongoDB BI connector."""
    server = details.get('server', 'cluster0.mongodb.net')
    database = details.get('database', 'mydb')
    collection = details.get('collection', table_name)
    m_query = 'let\n'
    m_query += f'    // Source MongoDB: {server}\n'
    m_query += f'    Source = MongoDBAtlas.Database("{server}", "{database}"),\n'
    m_query += f'    #"{collection}" = Source{{[Name="{collection}"]}}[Data],\n'
    m_query += f'    Result = #"{collection}"\nin\n    Result'
    return m_query


def _gen_m_cosmosdb(details, table_name, columns):
    """Azure Cosmos DB (SQL API or MongoDB API)."""
    endpoint = details.get('server', 'https://myaccount.documents.azure.com:443/')
    database = details.get('database', 'mydb')
    container = details.get('collection', table_name)
    m_query = 'let\n'
    m_query += f'    // Source Azure Cosmos DB: {endpoint}\n'
    m_query += f'    Source = DocumentDB.Contents("{endpoint}", "{database}"),\n'
    m_query += f'    #"{container}" = Source{{[Id="{container}"]}}[Data],\n'
    m_query += f'    Result = #"{container}"\nin\n    Result'
    return m_query


def _gen_m_athena(details, table_name, columns):
    """Amazon Athena via ODBC."""
    region = _odbc_escape(details.get('region', details.get('server', 'us-east-1')))
    catalog = _m_escape_string(details.get('catalog', details.get('database', 'AwsDataCatalog')))
    custom_sql = details.get('custom_sql', '')
    safe_table = _m_escape_string(table_name)
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source Amazon Athena: {region}\n'
        m_query += f'    Source = Odbc.Query("dsn=AmazonAthena;Region={region}",\n'
        m_query += f'        "{_m_escape_string(custom_sql)}")\n'
        m_query += 'in\n    Source'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Amazon Athena: {region}\n'
        m_query += f'    Source = Odbc.DataSource("dsn=AmazonAthena;Region={region}"),\n'
        m_query += f'    #"{catalog}" = Source{{[Name="{catalog}"]}}[Data],\n'
        m_query += f'    #"{safe_table} Table" = #"{catalog}"{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_db2(details, table_name, columns):
    """IBM DB2 connector."""
    server = details.get('server', 'localhost')
    database = details.get('database', 'SAMPLE')
    schema = details.get('schema', 'DB2INST1')
    return _gen_m_schema_item(details, table_name, columns,
                              'IBM DB2', 'DB2.Database', server, database, schema)


# ── Sprint 155: New Cloud & SaaS Connectors ──────────────────────────────────

def _gen_m_servicenow(details, table_name, columns):
    """ServiceNow via OData REST API."""
    instance = _m_escape_string(details.get('server', 'instance'))
    if not instance.startswith('https://'):
        instance = f'https://{instance}.service-now.com'
    table = _m_escape_string(details.get('_source_table', '') or table_name)
    m_query = 'let\n'
    m_query += f'    // Source ServiceNow OData: {instance}\n'
    m_query += f'    Source = OData.Feed("{instance}/api/now/table/{table}", null, '
    m_query += '[Implementation="2.0"]),\n'
    m_query += '    Result = Source\nin\n    Result'
    return m_query


def _gen_m_databricks_unity(details, table_name, columns):
    """Databricks Unity Catalog with catalog/schema/table navigation."""
    host = _m_escape_string(details.get('server', 'adb-workspace.azuredatabricks.net'))
    http_path = _m_escape_string(details.get('http_path', '/sql/1.0/warehouses/default'))
    catalog = _m_escape_string(details.get('catalog', 'main'))
    schema = _m_escape_string(details.get('schema', 'default'))
    safe_table = _m_escape_string(table_name)
    m_query = 'let\n'
    m_query += f'    // Source Databricks Unity Catalog: {host}\n'
    m_query += f'    Source = Databricks.Catalogs("{host}", "{http_path}", '
    m_query += f'[Catalog="{catalog}", Database="{schema}"]),\n'
    m_query += f'    #"{safe_table} Table" = Source{{[Name="{safe_table}"]}}[Data],\n'
    m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_denodo(details, table_name, columns):
    """Denodo Data Virtualization via ODBC."""
    server = _odbc_escape(details.get('server', 'localhost'))
    port = details.get('port', '9999')
    database = _odbc_escape(details.get('database', 'admin'))
    safe_table = _m_escape_string(table_name)
    m_query = 'let\n'
    m_query += f'    // Source Denodo: {server}:{port}\n'
    m_query += '    Source = Odbc.DataSource("DRIVER={DenodoODBC Unicode(x64)};'
    m_query += f'SERVER={server};PORT={port};DATABASE={database}"),\n'
    m_query += f'    #"{safe_table} Table" = Source{{[Name="{safe_table}"]}}[Data],\n'
    m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_essbase(details, table_name, columns):
    """Oracle Essbase / Hyperion via XMLA/ODBC bridge."""
    server = _odbc_escape(details.get('server', 'localhost'))
    application = _m_escape_string(details.get('application', details.get('database', 'Sample')))
    cube = _m_escape_string(details.get('cube', table_name))
    m_query = 'let\n'
    m_query += f'    // Source Oracle Essbase: {server} (requires XMLA provider + gateway)\n'
    m_query += '    // NOTE: Essbase requires On-Premises Data Gateway with XMLA/ODBC provider\n'
    m_query += f'    Source = Odbc.DataSource("DRIVER={{Essbase}};SERVER={server};'
    m_query += f'APPLICATION={application}"),\n'
    m_query += f'    #"{cube} Cube" = Source{{[Name="{cube}"]}}[Data],\n'
    m_query += f'    Result = #"{cube} Cube"\nin\n    Result'
    return m_query


def _gen_m_splunk(details, table_name, columns):
    """Splunk via REST API (Web.Contents)."""
    server = _m_escape_string(details.get('server', 'localhost'))
    port = details.get('port', '8089')
    search_query = _m_escape_string(details.get('custom_sql', 'search index=main | table *'))
    m_query = 'let\n'
    m_query += f'    // Source Splunk: {server}:{port}\n'
    m_query += '    // NOTE: Requires Splunk credentials in data source settings\n'
    m_query += f'    Source = Json.Document(Web.Contents("https://{server}:{port}'
    m_query += '/services/search/jobs/export", [\n'
    m_query += f'        Content=Text.ToBinary("search={search_query}&output_mode=json"),\n'
    m_query += '        Headers=[#"Content-Type"="application/x-www-form-urlencoded"]\n'
    m_query += '    ])),\n'
    m_query += '    #"Converted" = Table.FromRecords(Source[results]),\n'
    m_query += '    Result = #"Converted"\nin\n    Result'
    return m_query


def _gen_m_sap_hana_depth(details, table_name, columns):
    """SAP HANA with full schema/view navigation and MDX passthrough."""
    server = _m_escape_string(details.get('server', 'hanaserver'))
    port = details.get('port', '30015')
    schema = _m_escape_string(details.get('schema', 'SYSTEM'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')

    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source SAP HANA (Custom SQL): {server}:{port}\n'
        m_query += f'    Source = SapHana.Database("{server}:{port}", '
        m_query += '[Implementation="2.0"]),\n'
        m_query += '    #"Query" = Value.NativeQuery(Source, '
        m_query += f'"{_m_escape_string(custom_sql)}", null, '
        m_query += '[EnableFolding=true]),\n'
        m_query += '    Result = #"Query"\nin\n    Result'
    else:
        m_query = 'let\n'
        m_query += f'    // Source SAP HANA: {server}:{port}/{schema}\n'
        m_query += f'    Source = SapHana.Database("{server}:{port}", '
        m_query += '[Implementation="2.0"]),\n'
        m_query += f'    #"{schema}" = Source{{[Schema="{schema}"]}}[Data],\n'
        m_query += f'    #"{safe_table} Table" = #"{schema}"{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_redshift_depth(details, table_name, columns):
    """Amazon Redshift with schema navigation and Spectrum support."""
    server = _m_escape_string(details.get('server', 'cluster.region.redshift.amazonaws.com'))
    port = details.get('port', '5439')
    database = _m_escape_string(details.get('database', 'dev'))
    schema = _m_escape_string(details.get('schema', 'public'))
    safe_table = _m_escape_string(table_name)
    m_query = 'let\n'
    m_query += f'    // Source Amazon Redshift: {server}:{port}/{database}\n'
    m_query += f'    Source = AmazonRedshift.Database("{server}:{port}", "{database}"),\n'
    m_query += f'    #"{schema}" = Source{{[Name="{schema}"]}}[Data],\n'
    m_query += f'    #"{safe_table} Table" = #"{schema}"{{[Name="{safe_table}"]}}[Data],\n'
    m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


# ── Sprint 181: Enterprise Connector Expansion ───────────────────────────────

def _gen_m_dremio(details, table_name, columns):
    """Dremio lakehouse query engine via ODBC (Arrow Flight SQL driver)."""
    server = _odbc_escape(details.get('server', 'localhost'))
    port = details.get('port', '31010')
    schema = _m_escape_string(details.get('schema', details.get('database', 'space')))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source Dremio (Custom SQL): {server}:{port}\n'
        m_query += '    Source = Odbc.Query("DRIVER={Dremio Connector};'
        m_query += f'HOST={server};PORT={port}",\n'
        m_query += f'        "{_m_escape_string(custom_sql)}")\n'
        m_query += 'in\n    Source'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Dremio: {server}:{port}/{schema}\n'
        m_query += '    Source = Odbc.DataSource("DRIVER={Dremio Connector};'
        m_query += f'HOST={server};PORT={port}"),\n'
        m_query += f'    #"{schema}" = Source{{[Name="{schema}"]}}[Data],\n'
        m_query += f'    #"{safe_table} Table" = #"{schema}"{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_clickhouse(details, table_name, columns):
    """ClickHouse OLAP column store via ODBC."""
    server = _odbc_escape(details.get('server', 'localhost'))
    port = details.get('port', '8123')
    database = _odbc_escape(details.get('database', 'default'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source ClickHouse (Custom SQL): {server}:{port}\n'
        m_query += '    Source = Odbc.Query("DRIVER={ClickHouse ODBC Driver (Unicode)};'
        m_query += f'SERVER={server};PORT={port};DATABASE={database}",\n'
        m_query += f'        "{_m_escape_string(custom_sql)}")\n'
        m_query += 'in\n    Source'
    else:
        m_query = 'let\n'
        m_query += f'    // Source ClickHouse: {server}:{port}/{database}\n'
        m_query += '    Source = Odbc.DataSource("DRIVER={ClickHouse ODBC Driver (Unicode)};'
        m_query += f'SERVER={server};PORT={port};DATABASE={database}"),\n'
        m_query += f'    #"{safe_table} Table" = Source{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_singlestore(details, table_name, columns):
    """SingleStore (MemSQL) — MySQL wire-compatible distributed SQL."""
    server = details.get('server', 'localhost')
    port = details.get('port', '3306')
    database = details.get('database', 'memsql')
    return _gen_m_schema_item(details, table_name, columns,
                              f'SingleStore: {server}:{port}', 'MySQL.Database',
                              f'{server}:{port}', database, database)


def _gen_m_firebolt(details, table_name, columns):
    """Firebolt cloud data warehouse via ODBC."""
    engine = _odbc_escape(details.get('server', 'my-engine.firebolt.io'))
    database = _odbc_escape(details.get('database', 'my_db'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source Firebolt (Custom SQL): {engine}\n'
        m_query += '    Source = Odbc.Query("DRIVER={Firebolt ODBC};'
        m_query += f'ENGINE={engine};DATABASE={database}",\n'
        m_query += f'        "{_m_escape_string(custom_sql)}")\n'
        m_query += 'in\n    Source'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Firebolt: {engine}/{database}\n'
        m_query += '    Source = Odbc.DataSource("DRIVER={Firebolt ODBC};'
        m_query += f'ENGINE={engine};DATABASE={database}"),\n'
        m_query += f'    #"{safe_table} Table" = Source{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_starburst(details, table_name, columns):
    """Starburst Enterprise / Galaxy (Trino distribution) via ODBC."""
    server = _odbc_escape(details.get('server', 'starburst.example.com'))
    port = details.get('port', '443')
    catalog = _m_escape_string(details.get('catalog', details.get('database', 'hive')))
    schema = _m_escape_string(details.get('schema', 'default'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source Starburst (Custom SQL): {server}:{port}\n'
        m_query += '    Source = Odbc.Query("DRIVER={Starburst ODBC Driver};'
        m_query += f'HOST={server};PORT={port};SSL=1",\n'
        m_query += f'        "{_m_escape_string(custom_sql)}")\n'
        m_query += 'in\n    Source'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Starburst: {server}:{port}/{catalog}/{schema}\n'
        m_query += '    Source = Odbc.DataSource("DRIVER={Starburst ODBC Driver};'
        m_query += f'HOST={server};PORT={port};SSL=1"),\n'
        m_query += f'    #"{catalog}" = Source{{[Name="{catalog}"]}}[Data],\n'
        m_query += f'    #"{schema}" = #"{catalog}"{{[Name="{schema}"]}}[Data],\n'
        m_query += f'    #"{safe_table} Table" = #"{schema}"{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_db2_depth(details, table_name, columns):
    """IBM Db2 with schema navigation and LUW/z-OS support."""
    server = _m_escape_string(details.get('server', 'localhost'))
    port = details.get('port', '50000')
    database = _m_escape_string(details.get('database', 'SAMPLE'))
    schema = _m_escape_string(details.get('schema', 'DB2INST1'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source IBM Db2 (Custom SQL): {server}:{port}/{database}\n'
        m_query += f'    Source = DB2.Database("{server}:{port}", "{database}", '
        m_query += f'[Query="{_m_escape_string(custom_sql)}"]),\n'
        m_query += '    Result = Source\nin\n    Result'
    else:
        m_query = 'let\n'
        m_query += f'    // Source IBM Db2: {server}:{port}/{database}/{schema}\n'
        m_query += f'    Source = DB2.Database("{server}:{port}", "{database}"),\n'
        m_query += f'    #"{schema}" = Source{{[Schema="{schema}"]}}[Data],\n'
        m_query += f'    #"{safe_table} Table" = #"{schema}"{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_teradata_depth(details, table_name, columns):
    """Teradata with database navigation and native query passthrough."""
    server = _m_escape_string(details.get('server', 'teradata.example.com'))
    database = _m_escape_string(details.get('database', 'DBC'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source Teradata (Custom SQL): {server}\n'
        m_query += f'    Source = Teradata.Database("{server}", '
        m_query += f'[Query="{_m_escape_string(custom_sql)}"]),\n'
        m_query += '    Result = Source\nin\n    Result'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Teradata: {server}/{database}\n'
        m_query += f'    Source = Teradata.Database("{server}"),\n'
        m_query += f'    #"{database}" = Source{{[Name="{database}"]}}[Data],\n'
        m_query += f'    #"{safe_table} Table" = #"{database}"{{[Name="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query


def _gen_m_synapse_depth(details, table_name, columns):
    """Azure Synapse Analytics (dedicated/serverless SQL pool) with schema nav."""
    server = _m_escape_string(details.get('server', 'workspace.sql.azuresynapse.net'))
    database = _m_escape_string(details.get('database', 'pool'))
    schema = _m_escape_string(details.get('schema', 'dbo'))
    safe_table = _m_escape_string(table_name)
    custom_sql = details.get('custom_sql', '')
    if custom_sql:
        m_query = 'let\n'
        m_query += f'    // Source Azure Synapse (Custom SQL): {server}/{database}\n'
        m_query += f'    Source = Sql.Database("{server}", "{database}", '
        m_query += f'[Query="{_m_escape_string(custom_sql)}"]),\n'
        m_query += '    Result = Source\nin\n    Result'
    else:
        m_query = 'let\n'
        m_query += f'    // Source Azure Synapse: {server}/{database}/{schema}\n'
        m_query += f'    Source = Sql.Database("{server}", "{database}"),\n'
        m_query += f'    #"{safe_table} Table" = Source{{[Schema="{schema}", Item="{safe_table}"]}}[Data],\n'
        m_query += f'    Result = #"{safe_table} Table"\nin\n    Result'
    return m_query



_M_GENERATORS = {
    'Excel':            _gen_m_excel,
    'SQL Server':       _gen_m_sql_server,
    'PostgreSQL':       _gen_m_postgresql,
    'CSV':              _gen_m_csv,
    'BigQuery':         _gen_m_bigquery,
    'MySQL':            _gen_m_mysql,
    'Oracle':           _gen_m_oracle,
    'Snowflake':        _gen_m_snowflake,
    'GeoJSON':          _gen_m_geojson,
    'Teradata':         _gen_m_teradata,
    'SAP HANA':         _gen_m_sap_hana,
    'SAP BW':           _gen_m_sap_bw,
    'Amazon Redshift':  _gen_m_redshift,
    'Redshift':         _gen_m_redshift,
    'Databricks':       _gen_m_databricks,
    'Spark SQL':        _gen_m_spark,
    'Spark':            _gen_m_spark,
    'Azure SQL':        _gen_m_azure_sql,
    'Azure Synapse':    _gen_m_synapse,
    'Synapse':          _gen_m_synapse,
    'Google Sheets':    _gen_m_google_sheets,
    'SharePoint':       _gen_m_sharepoint,
    'JSON':             _gen_m_json,
    'XML':              _gen_m_xml,
    'PDF':              _gen_m_pdf,
    'Salesforce':       _gen_m_salesforce,
    'Web':              _gen_m_web,
    'Custom SQL':       _gen_m_custom_sql,
    'OData':            _gen_m_odata,
    'Google Analytics': _gen_m_google_analytics,
    'Azure Blob':       _gen_m_azure_blob,
    'Azure Blob Storage': _gen_m_azure_blob,
    'ADLS':             _gen_m_azure_blob,
    'Azure Data Lake':  _gen_m_azure_blob,
    'Vertica':          _gen_m_vertica,
    'Impala':           _gen_m_impala,
    'Hadoop Hive':      _gen_m_hadoop_hive,
    'Hive':             _gen_m_hadoop_hive,
    'HDInsight':        _gen_m_hadoop_hive,
    'Presto':           _gen_m_presto,
    'Trino':            _gen_m_presto,
    'Fabric Lakehouse': _gen_m_fabric_lakehouse,
    'Lakehouse':        _gen_m_fabric_lakehouse,
    'Dataverse':        _gen_m_dataverse,
    'Common Data Service': _gen_m_dataverse,
    'CDS':              _gen_m_dataverse,
    'hyper':            _gen_m_hyper,
    'Hyper':            _gen_m_hyper,
    'extract':          _gen_m_hyper,
    'Tableau Server':   _gen_m_sqlproxy,
    'sqlproxy':         _gen_m_sqlproxy,
    'SQLPROXY':         _gen_m_sqlproxy,
    # Sprint 61: New connectors
    'MongoDB':          _gen_m_mongodb,
    'MongoDB Atlas':    _gen_m_mongodb,
    'mongodb':          _gen_m_mongodb,
    'Cosmos DB':        _gen_m_cosmosdb,
    'Azure Cosmos DB':  _gen_m_cosmosdb,
    'cosmosdb':         _gen_m_cosmosdb,
    'DocumentDB':       _gen_m_cosmosdb,
    'Amazon Athena':    _gen_m_athena,
    'Athena':           _gen_m_athena,
    'athena':           _gen_m_athena,
    'IBM DB2':          _gen_m_db2,
    'DB2':              _gen_m_db2,
    'db2':              _gen_m_db2,
    # Sprint 155: New Cloud & SaaS connectors
    'ServiceNow':       _gen_m_servicenow,
    'servicenow':       _gen_m_servicenow,
    'Databricks Unity': _gen_m_databricks_unity,
    'databricks-unity-catalog': _gen_m_databricks_unity,
    'Denodo':           _gen_m_denodo,
    'denodo':           _gen_m_denodo,
    'Essbase':          _gen_m_essbase,
    'Oracle Essbase':   _gen_m_essbase,
    'Hyperion':         _gen_m_essbase,
    'essbase':          _gen_m_essbase,
    'Splunk':           _gen_m_splunk,
    'splunk':           _gen_m_splunk,
    'SAP HANA Deep':    _gen_m_sap_hana_depth,
    'Redshift Deep':    _gen_m_redshift_depth,
    # Sprint 181: Enterprise connector expansion
    'Dremio':           _gen_m_dremio,
    'dremio':           _gen_m_dremio,
    'ClickHouse':       _gen_m_clickhouse,
    'clickhouse':       _gen_m_clickhouse,
    'SingleStore':      _gen_m_singlestore,
    'singlestore':      _gen_m_singlestore,
    'MemSQL':           _gen_m_singlestore,
    'Firebolt':         _gen_m_firebolt,
    'firebolt':         _gen_m_firebolt,
    'Starburst':        _gen_m_starburst,
    'starburst':        _gen_m_starburst,
    'Starburst Galaxy': _gen_m_starburst,
    'Trino Enterprise': _gen_m_starburst,
    'Db2 Deep':         _gen_m_db2_depth,
    'IBM Db2 Deep':     _gen_m_db2_depth,
    'Teradata Deep':    _gen_m_teradata_depth,
    'Synapse Deep':     _gen_m_synapse_depth,
    'Azure Synapse Deep': _gen_m_synapse_depth,
}

# Required connection metadata, not credentials or a DirectQuery certification.
_DATABASE_REQUIRED_DETAILS = {
    _gen_m_sql_server: ('server', 'database'),
    _gen_m_postgresql: ('server', 'database'),
    _gen_m_mysql: ('server', 'database'),
    _gen_m_oracle: ('server', 'service'),
    _gen_m_snowflake: ('server', 'database', 'warehouse'),
    _gen_m_bigquery: ('project', 'dataset'),
    _gen_m_teradata: ('server', 'database'),
    _gen_m_sap_hana: ('server',),
    _gen_m_redshift: ('server', 'database'),
    _gen_m_databricks: ('server', 'http_path', 'catalog', 'schema'),
    _gen_m_spark: ('server',),
    _gen_m_azure_sql: ('server', 'database'),
    _gen_m_synapse: ('server', 'database'),
    _gen_m_vertica: ('server', 'database'),
    _gen_m_impala: ('server',),
    _gen_m_hadoop_hive: ('server',),
    _gen_m_presto: ('server', 'catalog', 'schema'),
    _gen_m_db2: ('server', 'database'),
    _gen_m_databricks_unity: ('server', 'http_path', 'catalog', 'schema'),
    _gen_m_denodo: ('server', 'database'),
    _gen_m_sap_hana_depth: ('server',),
    _gen_m_redshift_depth: ('server', 'database'),
    _gen_m_dremio: ('server', 'schema'),
    _gen_m_clickhouse: ('server', 'database'),
    _gen_m_singlestore: ('server', 'database'),
    _gen_m_firebolt: ('server', 'database'),
    _gen_m_starburst: ('server', 'catalog', 'schema'),
    _gen_m_db2_depth: ('server', 'database'),
    _gen_m_teradata_depth: ('server', 'database'),
    _gen_m_synapse_depth: ('server', 'database'),
}


def _m_generator(connection_type):
    if not isinstance(connection_type, str):
        return None
    exact = _M_GENERATORS.get(connection_type)
    if exact is not None:
        return exact
    normalized = connection_type.strip().casefold()
    return next((generator for name, generator in _M_GENERATORS.items()
                 if name.casefold() == normalized), None)


def native_database_binding_issues(connection):
    """Return missing DB metadata, or None for a non-database connector.

    Eligibility uses the actual M emitter, never a server attribute alone.
    No source is opened, and missing authentication is a refresh concern.
    """
    generator = _m_generator(connection.get('type'))
    if generator is None:
        return None
    required = _DATABASE_REQUIRED_DETAILS.get(generator)
    if required is None:
        return None
    details = connection.get('details', {})
    return [
        key for key in required
        if not isinstance(details.get(key), str) or not details[key].strip()
    ]


def _normalize_native_database_columns(query, columns):
    """Expose validated Tableau locals before captions and calculated steps."""
    physical = [column.get('sourceColumn') or column['name'] for column in columns]
    owners = {}
    for column, source in zip(columns, physical):
        if source in owners and owners[source] != column['name']:
            raise ValueError(
                "Native database physical column maps to multiple local fields")
        owners[source] = column['name']
    renames = {
        column['sourceColumn']: column['name']
        for column in columns
        if column.get('sourceColumn')
        and column['sourceColumn'] != column['name']
    }
    if not renames:
        return query
    steps: list[tuple[str, str]] = [m_transform_select_columns(physical)]
    if set(renames.values()) & set(physical):
        # A local name may already be another physical field (including swaps).
        taken = set(physical) | set(renames.values())
        temporary = {}
        for index, name in enumerate(renames):
            candidate = f'__TableauNativeColumn_{index}'
            while candidate in taken:
                candidate += '_'
            taken.add(candidate)
            temporary[name] = candidate
        steps.append(m_transform_rename(temporary))
        renames = {temporary[name]: target for name, target in renames.items()}
    steps.append(m_transform_rename(renames))
    return inject_m_steps(query, steps)


# ── Public API ────────────────────────────────────────────────────────────────

def generate_power_query_m(connection, table, *, use_original_file_paths=False):
    """
    Generates a Power Query M query from a Tableau connection.

    Args:
        connection: Dict with connection type and details
        table: Dict with table name and columns
        use_original_file_paths: Keep native file bindings at their declared
            locations instead of DataFolder; relative paths use the optional
            ``details['_source_base_dir']`` context. Generate M only, without
            file I/O. Defaults to False for staged/Fabric callers. Hyper is
            unaffected.

    Returns:
        str: Complete M query
    """
    conn_type = connection.get('type', 'Unknown')
    details = connection.get('details', {})
    table_name = table.get('name', 'Table1')
    columns = table.get('columns', [])

    source_table = table.get('source_table', '')
    if source_table:
        details = dict(details)
        details['_source_table'] = source_table

    generator = _m_generator(conn_type)
    if generator:
        if (generator is _gen_m_excel and source_table
                and '].[' in source_table and not table.get('connection_details')):
            logger.warning(
                "Excel table %r has a qualified source relation %r but no per-table "
                "connection provenance. Inherited workbook binding is unverified; "
                "exact worksheet navigation must succeed at refresh.",
                table_name, source_table,
            )
        if use_original_file_paths and generator in (
                _gen_m_excel, _gen_m_csv, _gen_m_geojson,
                _gen_m_json, _gen_m_xml, _gen_m_pdf):
            return generator(details, table_name, columns, use_original_file_paths=True)
        database_source = generator in _DATABASE_REQUIRED_DETAILS
        query_columns = (
            [dict(column, name=column.get('sourceColumn') or column['name'])
             for column in columns] if database_source else columns)
        query_table = table_name
        if (source_table and generator in _DATABASE_REQUIRED_DETAILS
                and generator not in {
                    _gen_m_sql_server, _gen_m_postgresql, _gen_m_mysql,
                    _gen_m_oracle, _gen_m_azure_sql, _gen_m_synapse,
                    _gen_m_singlestore,
                }):
            # Some emitters historically navigated by the model caption.
            # Native selection must instead address the declared physical leaf.
            schema, physical_name = _sql_source_identity(
                details, table_name, details.get('schema', ''))
            details = dict(details)
            if schema:
                details['schema'] = schema
                if generator is _gen_m_bigquery:
                    details['dataset'] = schema
            query_table = physical_name
        query = generator(details, query_table, query_columns)
        if database_source:
            query = _normalize_native_database_columns(query, columns)
        return query

    # Fallback — pass conn_type through details for the message
    details_copy = dict(details)
    details_copy['_conn_type'] = conn_type
    return _gen_m_fallback(details_copy, table_name, columns)


# ── Connection String Templating ─────────────────────────────────────────────

import re as _re


def apply_connection_template(m_query, env_vars=None):
    """Replace ${ENV.NAME} placeholders in M queries with environment variable values.

    Allows parameterizing M queries for different environments (dev/staging/prod).
    If env_vars is None, replaces with M parameter references instead.

    Supported placeholders:
        ${ENV.SERVER}     - Database server hostname
        ${ENV.DATABASE}   - Database name
        ${ENV.PORT}       - Port number
        ${ENV.USERNAME}   - Username
        ${ENV.PASSWORD}   - Password
        ${ENV.WAREHOUSE}  - Snowflake/Databricks warehouse
        ${ENV.SCHEMA}     - Database schema
        ${ENV.ACCOUNT}    - Storage account name
        ${ENV.CONTAINER}  - Blob/ADLS container
        ${ENV.CATALOG}    - Databricks/BigQuery catalog
        ${ENV.URL}        - Web/API URL
        Any custom ${ENV.XXXX} patterns

    Args:
        m_query: M query string potentially containing ${ENV.*} placeholders
        env_vars: Optional dict mapping env var names to values.
                  If None, generates M parameter references.

    Returns:
        str: M query with placeholders replaced
    """
    if not m_query or '${ENV.' not in m_query:
        return m_query

    def _replacer(match):
        var_name = match.group(1)
        if env_vars and var_name in env_vars:
            return env_vars[var_name]
        # Default: replace with M parameter reference
        return '" & ' + var_name + ' & "'

    return _re.sub(r'\$\{ENV\.([A-Za-z_]+)\}', _replacer, m_query)


def templatize_m_query(m_query, connection=None):
    """Convert hardcoded connection strings in an M query to ${ENV.*} templates.

    This is the reverse of apply_connection_template — it turns concrete
    server/database values into environment variable placeholders so the
    generated M queries can be parameterized per environment.

    Args:
        m_query: Concrete M query with hardcoded connection values
        connection: Optional connection dict with 'details' to identify values

    Returns:
        str: M query with connection values replaced by ${ENV.*} placeholders
    """
    if not m_query or not connection:
        return m_query

    details = connection.get('details', {})
    replacements = []

    # Build replacement list (longest values first to avoid partial matches)
    for key, env_var in [
        ('server', 'SERVER'), ('database', 'DATABASE'), ('port', 'PORT'),
        ('warehouse', 'WAREHOUSE'), ('schema', 'SCHEMA'),
        ('account', 'ACCOUNT'), ('container', 'CONTAINER'),
        ('catalog', 'CATALOG'), ('project', 'PROJECT'),
        ('dataset', 'DATASET'), ('http_path', 'HTTP_PATH'),
        ('site_url', 'SITE_URL'), ('url', 'URL'),
    ]:
        value = details.get(key, '')
        if value and len(value) > 2:
            replacements.append((value, f'${{ENV.{env_var}}}'))

    # Sort by length descending to replace longer values first
    replacements.sort(key=lambda x: -len(x[0]))

    result = m_query
    for old_val, new_val in replacements:
        result = result.replace(old_val, new_val)

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Tableau Prep Transformation Helpers — Power Query M Step Generators
# ══════════════════════════════════════════════════════════════════════════════
# These functions generate Power Query M transformation steps corresponding
# to Tableau Prep operations. They return (step_name, step_expression) tuples
# that can be injected into any source query via inject_m_steps().
#
# Usage pattern:
#   m_query = generate_power_query_m(connection, table)
#   steps = []
#   steps.append(m_transform_rename({"old_name": "new_name"}))
#   steps.append(m_transform_filter_values("Status", ["Active"]))
#   m_query = inject_m_steps(m_query, steps)
# ══════════════════════════════════════════════════════════════════════════════


def inject_m_steps(m_query, steps):
    """
    Inject additional M transformation steps into an existing M query.
    Inserts steps before the final 'in' clause.

    Can be called multiple times on the same query — previous Result =
    terminators are stripped and re-created at the new end.

    Args:
        m_query: str — Complete M query (let ... in ...)
        steps: list[tuple[str, str]] — (step_name, step_expression) pairs.
            Use {prev} placeholder in expressions to reference the previous step.

    Returns:
        str — Modified M query with additional steps injected
    """
    if not steps:
        return m_query

    # Find the last 'in\n' in the query
    in_idx = m_query.rfind('\nin\n')
    if in_idx == -1:
        in_idx = m_query.rfind('\nin ')
    if in_idx == -1:
        return m_query  # malformed query, return as-is

    before_in = m_query[:in_idx]

    # Strip any existing "Result = ..." line (from previous inject_m_steps call)
    lines = before_in.split('\n')
    while lines and lines[-1].strip().startswith('Result'):
        lines.pop()
    # Strip trailing comma from the last real step.
    # Must account for // line comments — a comma inside a comment is NOT actual M syntax.
    # IMPORTANT: Only strip // if it appears AFTER the step's closing paren/comma,
    # not inside the expression body (e.g., Tableau formulas may contain // comments
    # embedded in the expression text).
    if lines:
        last_line = lines[-1].rstrip()
        # Check if line already ends with comma (most common case)
        if not last_line.endswith(','):
            # Look for // only at the very end of the line (after closing paren + comma)
            # Use a conservative approach: find the last '),' or just ')' and check after
            last_close_paren = last_line.rfind(')')
            if last_close_paren >= 0:
                after_paren = last_line[last_close_paren + 1:].strip()
                if after_paren.startswith('//') or after_paren == '':
                    # The // is a trailing comment after the expression — safe to strip
                    code_part = last_line[:last_close_paren + 1].rstrip()
                    if not code_part.endswith(','):
                        code_part += ','
                    lines[-1] = code_part
                else:
                    lines[-1] = last_line + ','
            else:
                lines[-1] = last_line + ','
    before_in = '\n'.join(lines)

    # Find the last step name referenced (skip Result and comments)
    last_step = None
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith('Result') or stripped.startswith('//') or not stripped:
            continue
        if '=' in stripped:
            last_step = stripped.split('=')[0].strip().rstrip(',')
            break
    if not last_step:
        last_step = 'Source'

    # Collect existing step names to avoid duplicates
    existing_steps = set()
    for line in lines:
        stripped = line.strip()
        if '=' in stripped and not stripped.startswith('//'):
            name = stripped.split('=')[0].strip().rstrip(',')
            existing_steps.add(name)

    # Build the chain — replace {prev} with actual previous step name
    prev_step = last_step
    new_lines = []
    for step_name, step_expr_template in steps:
        # Deduplicate step name — append numeric suffix if collision
        unique_name = step_name
        counter = 2
        while unique_name in existing_steps:
            unique_name = f'{step_name} {counter}'
            if step_name.startswith('#"') and step_name.endswith('"'):
                unique_name = f'{step_name[:-1]} {counter}"'
            counter += 1
        existing_steps.add(unique_name)
        step_expr = step_expr_template.replace('{prev}', prev_step)
        new_lines.append(f'    {unique_name} = {step_expr},')
        prev_step = unique_name

    injected = '\n'.join(new_lines)
    return before_in + '\n' + injected + '\n    Result = ' + prev_step + '\nin\n    Result'


# ── Column operations ─────────────────────────────────────────────────────────

def m_transform_rename(renames):
    """Rename columns. renames: dict {old_name: new_name}"""
    pairs = ', '.join([f'{{{_m_literal(old)}, {_m_literal(new)}}}'
                       for old, new in renames.items()])
    return ('#"Renamed Columns"', f'Table.RenameColumns({{prev}}, {{{pairs}}})')


def m_transform_remove_columns(columns):
    """Remove specified columns."""
    cols = ', '.join([_m_literal(c) for c in columns])
    return ('#"Removed Columns"', f'Table.RemoveColumns({{prev}}, {{{cols}}})')


def m_transform_select_columns(columns):
    """Keep only specified columns."""
    cols = ', '.join([_m_literal(c) for c in columns])
    return ('#"Selected Columns"', f'Table.SelectColumns({{prev}}, {{{cols}}})')


def m_transform_duplicate_column(source_col, new_col):
    """Duplicate a column."""
    return ('#"Duplicated Column"',
            f'Table.DuplicateColumn({{prev}}, {_m_literal(source_col)}, '
            f'{_m_literal(new_col)})')


def m_transform_reorder_columns(column_order):
    """Reorder columns."""
    cols = ', '.join([_m_literal(c) for c in column_order])
    return ('#"Reordered Columns"', f'Table.ReorderColumns({{prev}}, {{{cols}}})')


def m_transform_split_by_delimiter(column, delimiter, num_parts=None):
    """Split column by delimiter."""
    name = f'#"Split {column}"'
    if num_parts:
        return (name,
                f'Table.SplitColumn({{prev}}, {_m_literal(column)}, '
                f'Splitter.SplitTextByDelimiter({_m_literal(delimiter)}, QuoteStyle.None), {num_parts})')
    return (name,
            f'Table.SplitColumn({{prev}}, {_m_literal(column)}, '
            f'Splitter.SplitTextByDelimiter({_m_literal(delimiter)}, QuoteStyle.None))')


def m_transform_merge_columns(columns, new_name, separator=" "):
    """Merge multiple columns into one."""
    cols = ', '.join([_m_literal(c) for c in columns])
    return ('#"Merged Columns"',
            f'Table.CombineColumns({{prev}}, {{{cols}}}, '
            f'Combiner.CombineTextByDelimiter({_m_literal(separator)}, QuoteStyle.None), '
            f'{_m_literal(new_name)})')


# ── Value operations ──────────────────────────────────────────────────────────

def m_transform_replace_value(column, old_value, new_value, replace_text=True):
    """Replace values in a column."""
    replacer = 'Replacer.ReplaceText' if replace_text else 'Replacer.ReplaceValue'
    old_repr = _m_literal(old_value) if isinstance(old_value, str) else ('null' if old_value is None else str(old_value))
    new_repr = _m_literal(new_value) if isinstance(new_value, str) else ('null' if new_value is None else str(new_value))
    return ('#"Replaced Values"',
            f'Table.ReplaceValue({{prev}}, {old_repr}, {new_repr}, {replacer}, {{"{column}"}})')


def m_transform_replace_nulls(column, default_value):
    """Replace null values with a default."""
    val_repr = _m_literal(default_value) if isinstance(default_value, str) else str(default_value)
    return (f'#"Replaced Nulls in {column}"',
            f'Table.ReplaceValue({{prev}}, null, {val_repr}, Replacer.ReplaceValue, {{"{column}"}})')


def _m_text_transform(columns, m_func, step_label):
    """Generic text column transform — shared by trim/clean/upper/lower/proper."""
    transforms = ', '.join([f'{{"{c}", {m_func}}}' for c in columns])
    return (f'#"{step_label}"', f'Table.TransformColumns({{prev}}, {{{transforms}}})')


def m_transform_trim(columns):
    """Trim whitespace from text columns."""
    return _m_text_transform(columns, 'Text.Trim', 'Trimmed Text')


def m_transform_clean(columns):
    """Remove non-printable characters from text columns."""
    return _m_text_transform(columns, 'Text.Clean', 'Cleaned Text')


def m_transform_upper(columns):
    """Convert text columns to uppercase."""
    return _m_text_transform(columns, 'Text.Upper', 'Uppercased')


def m_transform_lower(columns):
    """Convert text columns to lowercase."""
    return _m_text_transform(columns, 'Text.Lower', 'Lowercased')


def m_transform_proper_case(columns):
    """Convert text columns to proper case (Title Case)."""
    return _m_text_transform(columns, 'Text.Proper', 'Proper Cased')


def m_transform_fill_down(columns):
    """Fill down null values in columns."""
    cols = ', '.join([f'"{c}"' for c in columns])
    return ('#"Filled Down"', f'Table.FillDown({{prev}}, {{{cols}}})')


def m_transform_fill_up(columns):
    """Fill up null values in columns."""
    cols = ', '.join([f'"{c}"' for c in columns])
    return ('#"Filled Up"', f'Table.FillUp({{prev}}, {{{cols}}})')


# ── Filter operations ─────────────────────────────────────────────────────────

def m_transform_filter_values(column, keep_values):
    """Keep only rows where column matches specified values (categorical)."""
    if not keep_values:
        return ('#"Filtered Rows"', '{prev}')
    if len(keep_values) == 1:
        condition = f'each {_m_field_ref(column)} = {_m_literal(keep_values[0])}'
    else:
        vals = ', '.join([_m_literal(v) for v in keep_values])
        condition = f'each List.Contains({{{vals}}}, {_m_field_ref(column)})'
    return ('#"Filtered Rows"', f'Table.SelectRows({{prev}}, {condition})')


def m_transform_exclude_values(column, exclude_values):
    """Exclude rows where column matches specified values."""
    if len(exclude_values) == 1:
        condition = f'each {_m_field_ref(column)} <> {_m_literal(exclude_values[0])}'
    else:
        vals = ', '.join([_m_literal(v) for v in exclude_values])
        condition = f'each not List.Contains({{{vals}}}, {_m_field_ref(column)})'
    return ('#"Excluded Rows"', f'Table.SelectRows({{prev}}, {condition})')


def m_transform_filter_range(column, min_val=None, max_val=None):
    """Keep rows in a numeric or date range."""
    conditions = []
    if min_val is not None:
        conditions.append(f'{_m_field_ref(column)} >= {min_val}')
    if max_val is not None:
        conditions.append(f'{_m_field_ref(column)} <= {max_val}')
    condition = ' and '.join(conditions) if conditions else 'true'
    return ('#"Filtered Range"', f'Table.SelectRows({{prev}}, each {condition})')


def m_transform_filter_nulls(column, keep_nulls=False):
    """Filter null or non-null values."""
    op = '=' if keep_nulls else '<>'
    return ('#"Filtered Nulls"', f'Table.SelectRows({{prev}}, each {_m_field_ref(column)} {op} null)')


def m_transform_filter_contains(column, text):
    """Keep rows where column contains text (wildcard match)."""
    return ('#"Filtered Contains"',
            f'Table.SelectRows({{prev}}, each Text.Contains({_m_field_ref(column)}, {_m_literal(text)}))')


def m_transform_distinct(columns=None):
    """Remove duplicates. If columns specified, deduplicate on those columns only."""
    if columns:
        cols = ', '.join([f'"{c}"' for c in columns])
        return ('#"Removed Duplicates"', f'Table.Distinct({{prev}}, {{{cols}}})')
    return ('#"Removed Duplicates"', 'Table.Distinct({prev})')


def m_transform_top_n(n, sort_column, descending=True):
    """Keep top N rows by a column."""
    order = 'Order.Descending' if descending else 'Order.Ascending'
    return ('#"Top N"',
            f'Table.FirstN(Table.Sort({{prev}}, {{{{"{sort_column}", {order}}}}}), {n})')


# ── Aggregate operations ──────────────────────────────────────────────────────

_M_AGG_MAP = {
    'sum':     ('List.Sum', 'type number'),
    'avg':     ('List.Average', 'type number'),
    'average': ('List.Average', 'type number'),
    'count':   ('Table.RowCount', 'Int64.Type'),
    'countd':  (None, 'Int64.Type'),  # special: List.Count(List.Distinct(...))
    'min':     ('List.Min', 'type number'),
    'max':     ('List.Max', 'type number'),
    'median':  ('List.Median', 'type number'),
    'stdev':   ('List.StandardDeviation', 'type number'),
    'var':     (None, 'type number'),   # special: List.StandardDeviation² (sample variance)
    'varp':    (None, 'type number'),   # special: population variance via custom formula
}


def m_transform_aggregate(group_by_columns, aggregations):
    """
    Aggregate / Group By.
    Args:
        group_by_columns: list of column names to group by
        aggregations: list of dicts [{"name": "Total", "column": "Sales", "agg": "sum"}, ...]
    """
    group_cols = ', '.join([_m_literal(c) for c in group_by_columns])
    agg_parts = []
    for a in aggregations:
        name = a['name']
        col = a['column']
        agg = a['agg'].lower()
        output_name = _m_literal(name)
        field_ref = _m_field_ref(col)
        if agg == 'count':
            agg_parts.append(f'{{{output_name}, each Table.RowCount(_), Int64.Type}}')
        elif agg == 'countd':
            agg_parts.append(f'{{{output_name}, each List.Count(List.Distinct({field_ref})), Int64.Type}}')
        elif agg == 'var':
            # Sample variance = StdDev² (M has no built-in List.Variance)
            agg_parts.append(f'{{{output_name}, each Number.Power(List.StandardDeviation({field_ref}), 2), type number}}')
        elif agg == 'varp':
            # Population variance = avg((x - mean)²)
            agg_parts.append(
                f'{{{output_name}, each '
                f'List.Average(List.Transform({field_ref}, (x) => Number.Power(x - List.Average({field_ref}), 2))), '
                f'type number}}'
            )
        else:
            mapping = _M_AGG_MAP.get(agg, ('List.Sum', 'type number'))
            func, m_type = mapping
            agg_parts.append(f'{{{output_name}, each {func}({field_ref}), {m_type}}}')

    aggs = ', '.join(agg_parts)
    return ('#"Grouped Rows"', f'Table.Group({{prev}}, {{{group_cols}}}, {{{aggs}}})')


# ── Pivot / Unpivot operations ────────────────────────────────────────────────

def m_transform_unpivot(columns, attribute_name="Attribute", value_name="Value"):
    """Unpivot specific columns (columns become rows). Tableau Prep: Pivot Columns to Rows."""
    cols = ', '.join([_m_literal(c) for c in columns])
    return ('#"Unpivoted Columns"',
            f'Table.Unpivot({{prev}}, {{{cols}}}, {_m_literal(attribute_name)}, {_m_literal(value_name)})')


def m_transform_unpivot_other(keep_columns, attribute_name="Attribute", value_name="Value"):
    """Unpivot all columns except specified ones."""
    cols = ', '.join([_m_literal(c) for c in keep_columns])
    return ('#"Unpivoted Other Columns"',
            f'Table.UnpivotOtherColumns({{prev}}, {{{cols}}}, '
            f'{_m_literal(attribute_name)}, {_m_literal(value_name)})')


def m_transform_pivot(pivot_column, value_column, agg_function="List.Sum"):
    """Pivot rows to columns. Tableau Prep: Pivot Rows to Columns."""
    return ('#"Pivoted Column"',
            f'Table.Pivot({{prev}}, List.Distinct({{prev}}{_m_field_ref(pivot_column)}), '
            f'{_m_literal(pivot_column)}, {_m_literal(value_column)}, {agg_function})')


# ── Join operations ───────────────────────────────────────────────────────────

_M_JOIN_KIND = {
    'inner':      'JoinKind.Inner',
    'left':       'JoinKind.LeftOuter',
    'leftouter':  'JoinKind.LeftOuter',
    'right':      'JoinKind.RightOuter',
    'rightouter': 'JoinKind.RightOuter',
    'full':       'JoinKind.FullOuter',
    'fullouter':  'JoinKind.FullOuter',
    'leftanti':   'JoinKind.LeftAnti',
    'rightanti':  'JoinKind.RightAnti',
}


def m_transform_buffer(table_ref=None):
    """Buffer a table to force query-folding boundary.

    Wrapping a table reference in Table.Buffer() forces the engine to
    materialise the table before the next step.  This is useful before
    joins to prevent the engine from sending un-foldable join predicates
    to the data source.

    Args:
        table_ref: Optional M table reference to buffer.  When *None*,
                   the ``{prev}`` placeholder is used so the step can be
                   chained via ``inject_m_steps()``.
    Returns:
        (step_name, step_expression) tuple.
    """
    ref = table_ref or '{prev}'
    return ('#"Buffered Table"', f'Table.Buffer({ref})')


def m_transform_join(right_table_ref, left_keys, right_keys, join_type='left',
                     expand_columns=None, joined_name="Joined",
                     buffer_right=False):
    """
    Join two tables.
    Args:
        right_table_ref: str — M reference to the right table
        left_keys / right_keys: list of str — key columns
        join_type: str — inner, left, right, full, leftanti, rightanti
        expand_columns: list of str — columns to expand (None = no expansion step)
        joined_name: str — name of the joined nested column
        buffer_right: bool — when True, wrap right_table_ref in Table.Buffer()
                      to create a query-folding boundary (prevents the engine from
                      sending un-foldable join predicates to the data source)
    Returns:
        list of (step_name, step_expression) tuples (join + optional expand)
    """
    kind = _M_JOIN_KIND.get(join_type.lower().replace(' ', ''), 'JoinKind.LeftOuter')
    if len(left_keys) == 1:
        lk, rk = _m_literal(left_keys[0]), _m_literal(right_keys[0])
    else:
        lk = '{' + ', '.join([_m_literal(k) for k in left_keys]) + '}'
        rk = '{' + ', '.join([_m_literal(k) for k in right_keys]) + '}'

    effective_right = f'Table.Buffer({right_table_ref})' if buffer_right else right_table_ref

    steps = [(f'#"Joined {joined_name}"',
              f'Table.NestedJoin({{prev}}, {lk}, {effective_right}, {rk}, '
              f'{_m_literal(joined_name)}, {kind})')]
    if expand_columns:
        cols = ', '.join([_m_literal(c) for c in expand_columns])
        steps.append((f'#"Expanded {joined_name}"',
                       f'Table.ExpandTableColumn({{prev}}, {_m_literal(joined_name)}, {{{cols}}})'))
    return steps


# ── Union operations ──────────────────────────────────────────────────────────

def m_transform_union(table_refs):
    """Union (append) multiple tables. table_refs: list of M table references."""
    refs = ', '.join(table_refs)
    return ('#"Combined Tables"', f'Table.Combine({{{refs}}})')


def m_transform_wildcard_union(folder_path, file_extension=".csv", delimiter=","):
    """Union all matching files in a folder (Wildcard Union). Returns a complete M query."""
    folder_bs = folder_path.replace('/', '\\')
    return f'''let
    // Wildcard Union: all {file_extension} files in folder
    Source = Folder.Files("{folder_bs}"),
    #"Filtered Files" = Table.SelectRows(Source, each Text.EndsWith([Name], "{file_extension}")),
    #"Added Tables" = Table.AddColumn(#"Filtered Files", "ParsedTable",
        each Csv.Document([Content], [Delimiter="{delimiter}", Encoding=65001])),
    #"Combined" = Table.Combine(#"Added Tables"[ParsedTable]),
    #"Promoted Headers" = Table.PromoteHeaders(#"Combined", [PromoteAllScalars=true])
in
    #"Promoted Headers"'''


# ── Reshape operations ────────────────────────────────────────────────────────

def m_transform_sort(sort_specs):
    """Sort rows. sort_specs: list of (column, descending_bool) tuples."""
    sorts = ', '.join([
        f'{{"{col}", {"Order.Descending" if desc else "Order.Ascending"}}}'
        for col, desc in sort_specs
    ])
    return ('#"Sorted Rows"', f'Table.Sort({{prev}}, {{{sorts}}})')


def m_transform_transpose():
    """Transpose table (rows ↔ columns)."""
    return ('#"Transposed Table"', 'Table.Transpose({prev})')


def m_transform_add_index(column_name="Index", start=1, increment=1):
    """Add an index column."""
    return ('#"Added Index"',
            f'Table.AddIndexColumn({{prev}}, "{column_name}", {start}, {increment})')


def m_transform_skip_rows(n):
    """Remove first N rows."""
    return ('#"Skipped Rows"', f'Table.Skip({{prev}}, {n})')


def m_transform_remove_last_rows(n):
    """Remove last N rows."""
    return ('#"Removed Last Rows"', f'Table.RemoveLastN({{prev}}, {n})')


def m_transform_promote_headers():
    """Promote first row to headers."""
    return ('#"Promoted Headers"',
            'Table.PromoteHeaders({prev}, [PromoteAllScalars=true])')


def m_transform_demote_headers():
    """Demote headers to first row."""
    return ('#"Demoted Headers"', 'Table.DemoteHeaders({prev})')


# ── Calculated column ─────────────────────────────────────────────────────────

def m_transform_add_column(new_col_name, expression, col_type=None):
    """
    Add a calculated column.
    Args:
        new_col_name: str
        expression: str — M expression (e.g., 'each [Price] * [Qty]')
        col_type: str — optional M type (e.g., 'type number')
    """
    type_arg = f', {col_type}' if col_type else ''
    # Escape " in step name (M identifier quoting uses "" for literal quotes)
    safe_step_name = new_col_name.replace('"', '""')
    # Escape " in column name within the Table.AddColumn expression string
    safe_col_name = new_col_name.replace('"', '""')
    return (f'#"Added {safe_step_name}"',
            f'Table.AddColumn({{prev}}, "{safe_col_name}", {expression}{type_arg})')


def m_transform_conditional_column(new_col_name, conditions, default_value=None):
    """
    Add a conditional (IF/THEN/ELSE) column.
    Args:
        new_col_name: str
        conditions: list of (condition_expr, result_value) — e.g., [('[Sales] > 1000', '"High"')]
        default_value: str — default if no condition matches
    """
    expr = ""
    for cond, val in conditions:
        # Strip spurious 'each' prefix — each belongs in Table.AddColumn, not in conditions
        clean_cond = cond
        if clean_cond.startswith('each '):
            clean_cond = clean_cond[5:]
        expr += f'if {clean_cond} then {val} else '
    expr += str(default_value) if default_value is not None else 'null'
    return (f'#"Added {new_col_name}"',
            f'Table.AddColumn({{prev}}, "{new_col_name}", each {expr})')


# ── Error handling transforms ─────────────────────────────────────────────────

def m_transform_remove_errors(columns=None):
    """
    Remove rows containing errors.
    Args:
        columns: optional list of column names to check for errors.
                 If None, removes errors across all columns.
    """
    if columns:
        cols = ', '.join([f'"{c}"' for c in columns])
        return ('#"Removed Errors"',
                f'Table.RemoveRowsWithErrors({{prev}}, {{{cols}}})')
    return ('#"Removed Errors"', 'Table.RemoveRowsWithErrors({prev})')


def m_transform_replace_errors(columns, replacement=None):
    """
    Replace error values in specified columns with a replacement value.
    Args:
        columns: list of column names to process
        replacement: replacement value (default: null)
    """
    repl = str(replacement) if replacement is not None else 'null'
    transforms = ', '.join([f'{{"{c}", {repl}}}' for c in columns])
    return ('#"Replaced Errors"',
            f'Table.ReplaceErrorValues({{prev}}, {{{transforms}}})')


# ── Regex → M fallback transforms ─────────────────────────────────────────────

def m_regex_match(column, pattern):
    """Generate M step for regex match (returns boolean column).

    Uses ``Text.RegexMatch`` available in Power Query (December 2024+).
    Falls back to a ``Text.Contains`` approximation comment for older engines.
    """
    safe_col = _m_field_ref(column)
    return (f'#"Regex Match {_m_escape_col_name(column)}"',
            f'Table.AddColumn({{prev}}, {_m_literal(column + "_match")}, '
            f'each try Text.RegexMatch({safe_col}, {_m_literal(pattern)}) otherwise false, type logical)')


def m_regex_extract(column, pattern, new_column=None):
    """Generate M step for regex extract (captures first group).

    Uses ``Text.RegexExtract`` to pull the first capture group from *pattern*.
    """
    safe_col = _m_field_ref(column)
    out_col = new_column or f'{column}_extract'
    return (f'#"Regex Extract {_m_escape_col_name(column)}"',
            f'Table.AddColumn({{prev}}, {_m_literal(out_col)}, '
            f'each try Text.RegexExtract({safe_col}, {_m_literal(pattern)}) otherwise null, type text)')


def m_regex_replace(column, pattern, replacement):
    """Generate M step for regex replace.

    Uses ``Text.RegexReplace`` to substitute all matches of *pattern*.
    """
    safe_col = _m_field_ref(column)
    return (f'#"Regex Replace {_m_escape_col_name(column)}"',
            f'Table.TransformColumns({{prev}}, {{{{{_m_literal(column)}, '
            f'each try Text.RegexReplace({safe_col}, {_m_literal(pattern)}, '
            f'{_m_literal(replacement)}) otherwise {safe_col}}}}})')


def convert_tableau_regex_to_m(formula, column_name):
    """Convert a Tableau REGEXP_* formula to a Power Query M step tuple.

    Recognises REGEXP_MATCH, REGEXP_EXTRACT, REGEXP_EXTRACT_NTH, and
    REGEXP_REPLACE.  Returns a ``(step_name, step_expression)`` tuple that
    can be injected via ``inject_m_steps()``, or *None* if the formula does
    not contain a recognised REGEXP function.
    """
    import re as _re

    # REGEXP_MATCH(field, "pattern")
    m = _re.search(r'REGEXP_MATCH\s*\(\s*\[?([^\],]+?)\]?\s*,\s*["\'](.+?)["\']\s*\)', formula, _re.IGNORECASE)
    if m:
        return m_regex_match(m.group(1).strip(), m.group(2))

    # REGEXP_EXTRACT(field, "pattern")
    m = _re.search(r'REGEXP_EXTRACT\s*\(\s*\[?([^\],]+?)\]?\s*,\s*["\'](.+?)["\']\s*\)', formula, _re.IGNORECASE)
    if m:
        return m_regex_extract(m.group(1).strip(), m.group(2), new_column=column_name)

    # REGEXP_EXTRACT_NTH(field, "pattern", n)
    m = _re.search(r'REGEXP_EXTRACT_NTH\s*\(\s*\[?([^\],]+?)\]?\s*,\s*["\'](.+?)["\']\s*,\s*(\d+)\s*\)',
                    formula, _re.IGNORECASE)
    if m:
        pat_with_group = m.group(2)
        return m_regex_extract(m.group(1).strip(), pat_with_group,
                               new_column=column_name)

    # REGEXP_REPLACE(field, "pattern", "replacement")
    m = _re.search(r'REGEXP_REPLACE\s*\(\s*\[?([^\],]+?)\]?\s*,\s*["\'](.+?)["\']\s*,\s*["\'](.*)["\']\s*\)',
                    formula, _re.IGNORECASE)
    if m:
        return m_regex_replace(m.group(1).strip(), m.group(2), m.group(3))

    return None


def m_transform_try_otherwise(step_name, expression, fallback_expression):
    """
    Wrap a step expression in a try...otherwise block for graceful error handling.
    Args:
        step_name: the step name (e.g., '#"Connected Source"')
        expression: the primary M expression to attempt
        fallback_expression: the expression to use if the primary fails
    Returns:
        (step_name, wrapped_expression) tuple
    """
    return (step_name, f'try {expression} otherwise {fallback_expression}')


def wrap_source_with_try_otherwise(m_query, empty_table_columns=None):
    """Compatibility no-op: return the M query unchanged.

    Automatic Source fallbacks used to turn connection/navigation errors into
    successful empty refreshes. They are no longer injected for any connector.
    Keep this helper and its signature for existing callers; the legacy
    ``empty_table_columns`` argument is ignored. Authored ``try`` expressions
    and targeted value-level error handling are preserved verbatim.
    """
    return m_query


# ── Hyper data integration ────────────────────────────────────────────────────


def generate_m_from_hyper(hyper_tables, table_name=None):
    """Generate an M query using data from ``hyper_reader``.

    If the datasource has ``hyper_reader_tables`` (populated by
    ``extract_hyper_metadata``), this function produces an M expression
    with inline sample data or a CSV reference.

    Args:
        hyper_tables: list of table dicts from ``hyper_reader.read_hyper()``.
        table_name: Optional table name to match. If ``None``, uses the first.

    Returns:
        str | None: M expression, or ``None`` if no suitable data found.
    """
    if not hyper_tables:
        return None

    try:
        from hyper_reader import generate_m_for_hyper_table
    except ImportError:
        return None

    # Find matching table
    target = None
    for t in hyper_tables:
        if table_name and t.get('table', '').lower() == table_name.lower():
            target = t
            break
    if target is None:
        target = hyper_tables[0]

    if not target.get('columns'):
        return None

    return generate_m_for_hyper_table(target)


# ── Sprint 61: New Transform Generators ───────────────────────────────────────

def gen_extract_regex(column, pattern, group=0):
    """Regex extraction transform.

    Returns:
        Tuple (step_name, step_expression) with ``{prev}`` placeholder.
    """
    step_name = f'Regex_{column}'
    expr = (
        'Table.TransformColumns({prev}, {{'
        + _m_literal(column)
        + ', each try Text.RegexExtract(_, '
        + _m_literal(pattern)
        + f', {group}) otherwise null}})'
    )
    return (step_name, expr)


def gen_parse_json(column):
    """JSON parsing + record expansion transform.

    Returns:
        Tuple (step_name, step_expression) with ``{prev}`` placeholder.
    """
    step_name = f'ParseJSON_{column}'
    expr = (
        f'Table.TransformColumns({{prev}}, '
        f'{{{{"{column}", Json.Document}}}}'
        f')'
    )
    return (step_name, expr)


def gen_parse_xml(column):
    """XML parsing transform.

    Returns:
        Tuple (step_name, step_expression) with ``{prev}`` placeholder.
    """
    step_name = f'ParseXML_{column}'
    expr = (
        f'Table.TransformColumns({{prev}}, '
        f'{{{{"{column}", Xml.Tables}}}}'
        f')'
    )
    return (step_name, expr)


def parameterize_connection(m_expression, param_map=None):
    """Replace hardcoded connection values with Power Query parameter references.

    Args:
        m_expression: Complete M query string.
        param_map: Dict mapping placeholder names to PBI parameter names,
            e.g. ``{"ServerName": "P_Server", "DatabaseName": "P_Database"}``.

    Returns:
        Modified M expression with parameter references.
    """
    if not param_map:
        return m_expression

    result = m_expression
    for placeholder, pq_param in param_map.items():
        # Replace quoted values with parameter references
        result = result.replace(f'"{placeholder}"', f'#"{pq_param}"')
    return result


def generate_connection_parameters(connection_map):
    """Generate Power Query parameter M expressions for multi-connection workbooks.

    When a workbook connects to multiple databases, each unique connection
    gets its own server/database parameters to allow independent configuration.

    Args:
        connection_map: Dict of connection_name -> connection_details
            from datasource_extractor.

    Returns:
        list of dicts with ``name`` and ``m_expression`` for PBI parameters.
    """
    params = []
    seen_servers = {}  # (server, database) -> param_prefix
    idx = 0

    for conn_name, conn in connection_map.items():
        details = conn.get('details', {})
        server = details.get('server', '')
        database = details.get('database', '')
        if not server:
            continue

        key = (server, database)
        if key in seen_servers:
            continue  # Same connection already parameterized

        idx += 1
        prefix = f"Conn{idx}" if idx > 1 else ""
        seen_servers[key] = prefix

        # Server parameter
        params.append({
            'name': f'{prefix}ServerName' if prefix else 'ServerName',
            'm_expression': f'"{server}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
        })
        # Database parameter (if applicable)
        if database:
            params.append({
                'name': f'{prefix}DatabaseName' if prefix else 'DatabaseName',
                'm_expression': f'"{database}" meta [IsParameterQuery=true, Type="Text", IsParameterQueryRequired=true]',
            })

    return params


def generate_blend_merge_query(primary_query_name, secondary_query_name,
                                link_columns, join_kind='left'):
    """Generate a Power Query M merge step that blends two datasource queries.

    Simulates Tableau data blending by creating a Table.NestedJoin that links
    a primary query to a secondary query on the specified columns, then expands
    the secondary columns.

    Args:
        primary_query_name: Name of the primary M query (e.g. 'Orders')
        secondary_query_name: Name of the secondary M query (e.g. 'Returns')
        link_columns: List of dicts with 'primary' and 'secondary' column names
        join_kind: Join type — 'left' (default, matches Tableau blend), 'inner', 'full'

    Returns:
        str: Complete M query for the blended/merged result
    """
    if not link_columns:
        # No link columns — just combine (append)
        return (
            f'let\n'
            f'    Primary = {primary_query_name},\n'
            f'    Secondary = {secondary_query_name},\n'
            f'    Combined = Table.Combine({{Primary, Secondary}})\n'
            f'in\n'
            f'    Combined'
        )

    primary_keys = [lc.get('primary', lc.get('column', '')) for lc in link_columns]
    secondary_keys = [lc.get('secondary', lc.get('column', '')) for lc in link_columns]

    pk_list = ', '.join(f'"{k}"' for k in primary_keys)
    sk_list = ', '.join(f'"{k}"' for k in secondary_keys)

    kind_map = {
        'left': 'JoinKind.LeftOuter',
        'inner': 'JoinKind.Inner',
        'full': 'JoinKind.FullOuter',
        'right': 'JoinKind.RightOuter',
        'leftanti': 'JoinKind.LeftAnti',
    }
    m_join_kind = kind_map.get(join_kind, 'JoinKind.LeftOuter')

    return (
        f'let\n'
        f'    Primary = {primary_query_name},\n'
        f'    Secondary = {secondary_query_name},\n'
        f'    Merged = Table.NestedJoin(\n'
        f'        Primary, {{{pk_list}}},\n'
        f'        Secondary, {{{sk_list}}},\n'
        f'        "Blended", {m_join_kind}\n'
        f'    ),\n'
        f'    Expanded = Table.ExpandTableColumn(\n'
        f'        Merged, "Blended",\n'
        f'        Table.ColumnNames(Secondary)\n'
        f'    )\n'
        f'in\n'
        f'    Expanded'
    )


def generate_table_extension_query(extension):
    """Generate a Power Query M query for a Tableau table extension.

    Converts Tableau 2024.2+ table extensions (Einstein Discovery, external API)
    to Web.Contents() M queries or placeholder queries with migration notes.

    Args:
        extension: Dict from extract_table_extensions with name, extension_type,
                   endpoint, schema, config.

    Returns:
        str: M query string
    """
    name = extension.get('name', 'Extension')
    endpoint = extension.get('endpoint', '')
    ext_type = extension.get('extension_type', 'unknown')
    schema = extension.get('schema', [])

    if endpoint:
        # Generate Web.Contents query for API-based extensions
        col_types = []
        for col in schema:
            dt = col.get('datatype', 'string')
            m_type = {'integer': 'Int64.Type', 'real': 'Number.Type',
                      'boolean': 'Logical.Type', 'date': 'Date.Type',
                      'datetime': 'DateTime.Type'}.get(dt, 'Text.Type')
            col_types.append(f'{{"{col["name"]}", {m_type}}}')

        type_list = ', '.join(col_types) if col_types else ''
        lines = [
            'let',
            f'    // Table Extension: {name} (type: {ext_type})',
            f'    Source = Json.Document(Web.Contents("{endpoint}")),',
            '    AsTable = Table.FromRecords(Source)',
        ]
        if type_list:
            lines.append(f'    ,Typed = Table.TransformColumnTypes(AsTable, {{{type_list}}})')
            lines.append('in')
            lines.append('    Typed')
        else:
            lines.append('in')
            lines.append('    AsTable')
        return '\n'.join(lines)
    else:
        # Placeholder for extensions without a direct endpoint
        cols = ([f'"{_m_escape_col_name(c["name"])}"' for c in schema]
                if schema else ['"Value"'])
        return (
            f'let\n'
            f'    // MigrationNote: Tableau table extension "{name}" (type: {ext_type})\n'
            f'    // requires manual configuration — no direct endpoint available.\n'
            f'    Source = #table({{{", ".join(cols)}}}, {{}})\n'
            f'in\n'
            f'    Source'
        )
