"""Date/calendar dimension detection and the generated Calendar table.

A source that ships its own date dimension suppresses the generated Calendar,
so detection and generation belong together: ``_is_date_table`` decides, and
``_add_date_table`` builds the M-partitioned Calendar plus its relationship.
Extracted from ``tmdl_generator``, which re-exports these names.
"""

import re
from datetime import date, datetime


# Well-known date table names (any language)
_DATE_TABLE_NAMES = {
    # English
    'calendar', 'date', 'dimdate', 'dim_date', 'datedimension',
    'date_dimension', 'dim date', 'datetable', 'date_table',
    'time', 'dimtime', 'dim_time', 'dates',
    # French
    'calendrier', 'dimcalendrier', 'dim_calendrier',
    'tabledate', 'table_date', 'temps',
    # German
    'datum', 'kalender', 'dimdatum', 'dim_datum', 'dimkalender',
    'dim_kalender', 'zeit',
    # Spanish
    'fecha', 'calendario', 'dimfecha', 'dim_fecha', 'dimcalendario',
    'dim_calendario',
    # Portuguese
    'data', 'dimdata', 'dim_data',
    # Italian
    'datacalendario',
    # Dutch (datum/kalender already covered above)
    # Romanized
    'datemaster', 'date_master', 'masterdate', 'master_date',
}

# Column-name patterns that are typical date-part columns (any language)
_DATE_PART_PATTERNS = re.compile(
    r'^('
    # Year
    r'year|ann[eé]e|annee|jahr|a[nñ]o|ano|anno|jaar'
    r'|'
    # Month
    r'month|mois|monat|mes|mese|maand|monthname|month.?name|month.?num'
    r'|'
    # Day
    r'day|jour|tag|d[ií]a|dia|giorno|dag|dayname|day.?name|dayofweek|day.?of.?week'
    r'|'
    # Quarter
    r'quarter|trimestre|quartal|kwartaal|quarter.?name|quarter.?num'
    r'|'
    # Week
    r'week|semaine|woche|semana|settimana|weeknum|week.?num|weekday|week.?of.?year'
    r'|'
    # Date (the key column itself)
    r'date|datum|fecha|data|datekey|date.?key|fulldate|full.?date'
    r'|'
    # Calendar-specific
    r'calendar|calendrier|kalender|calendario|fiscal.?year|fiscal.?month|fiscal.?quarter'
    r')$', re.IGNORECASE
)

_TECHNICAL_DATE = re.compile(
    r'(?:^|[_ .-])(maj|mise[ _-]?a?jour|update|refresh|load|etl|insert|extract|'
    r'modif|modified|timestamp|time_stamp|ts)(?:$|[_ .-])', re.IGNORECASE)


def _date_column_score(name):
    """Prefer business dates over operational refresh timestamps."""
    lowered = str(name).casefold().replace('é', 'e').replace('è', 'e')
    score = 0
    if _TECHNICAL_DATE.search(lowered):
        score -= 10
    if re.search(r'(?:^|[_ .-])(date|jour|day|datum)(?:$|[_ .-])', lowered):
        score += 4
    if re.search(r'(?:^|[_ .-])(annee|year|month|mois|quarter|trimestre)', lowered):
        score += 2
    return score


def _is_date_table(table):
    """Detect whether a table is a date/calendar dimension table.

    Uses two strategies:
    1. Name-based: table name matches a known date table name (any language).
    2. Column-heuristic: table has a DateTime column AND ≥50% of its columns
       have names that match common date-part patterns (Year, Month, Day, etc.).
    """
    name = table.get('name', '').lower().strip()

    # Strategy 1: well-known name
    if name in _DATE_TABLE_NAMES:
        return True

    # Strategy 2: column heuristic
    columns = table.get('columns', [])
    if not columns:
        return False

    has_datetime_col = any(
        c.get('dataType') == 'DateTime' or c.get('dataCategory') == 'DateTime'
        for c in columns
    )
    if not has_datetime_col:
        return False

    date_part_count = sum(
        1 for c in columns
        if _DATE_PART_PATTERNS.match(c.get('name', '').strip())
    )

    # If ≥50% of columns look like date parts, it's a date table
    return date_part_count >= len(columns) * 0.5


def _worksheet_date_usage(model, worksheets, table_rename_map):
    """Resolve worksheet fields against their owning datasource, not globally.

    Return axis/date-part support and total worksheet support per physical
    date column. Repeated encodings in one worksheet count only once.
    """
    tables = {t['name']: t for t in model['model']['tables']}
    sources = model.get('_datasources', [])
    renames = model.get('_column_rename_map', {})
    usage = {}
    for ws in worksheets:
        supported = {}
        for field in ws.get('fields', []):
            name = field.get('name', '').strip('[]')
            ds_name = field.get('datasource', '')
            explicit_table = field.get('table', '')
            matches = set()
            for ds in sources:
                if ds_name and ds.get('name') != ds_name:
                    continue
                for source in ds.get('tables', []):
                    raw_table = source.get('name', '')
                    tname = table_rename_map.get((ds.get('name', ''), raw_table), raw_table)
                    if explicit_table and explicit_table not in (raw_table, tname):
                        continue
                    for col in source.get('columns', []):
                        raw = col.get('name', '').strip('[]')
                        if name not in (raw, col.get('caption')):
                            continue
                        produced = renames.get((tname, raw), col.get('caption') or raw)
                        matches.add((tname, produced))
            # Legacy callers may supply worksheets without datasource metadata.
            # Only use unqualified names when they identify exactly one table.
            if not sources and not ds_name:
                matches = {(tname, name) for tname, table in tables.items()
                           if any(c.get('name') == name for c in table.get('columns', []))}
            if not ds_name and not explicit_table and len(matches) > 1:
                continue
            axis = field.get('shelf') in ('rows', 'columns')
            rank = 2 if axis and (field.get('date_part') or field.get('derivation')) else int(axis)
            for key in matches:
                table = tables.get(key[0], {})
                if any(c.get('name') == key[1] and not c.get('isCalculated', False)
                       and (c.get('dataType') == 'DateTime' or c.get('dataCategory') == 'DateTime')
                       for c in table.get('columns', [])):
                    supported[key] = max(supported.get(key, 0), rank)
        for key, rank in supported.items():
            counts = usage.setdefault(key, [0, 0, 0])
            counts[0] += rank == 2
            counts[1] += rank >= 1
            counts[2] += 1
    return usage


def _source_date_years(model, hyper_files):
    """Read typed date bounds/embedded rows; never reference fact M queries.

    Statistics can cover the whole source; sample rows only cover the observed
    values. Missing data retains the historical fallback rather than guessing
    years from column names, numeric values, or unrelated text.
    """
    years = set()

    def add(value):
        if isinstance(value, (date, datetime)):
            years.add(value.year)
        elif isinstance(value, str) and re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[T ])', value):
            try:
                years.add(date.fromisoformat(value[:10]).year)
            except ValueError:
                pass

    def collect(table, stats_key):
        names = {c.get('name', '').strip('[]') for c in table.get('columns', [])
                 if str(c.get('datatype') or c.get('hyper_type') or c.get('dataType') or '').lower()
                 in ('date', 'datetime', 'timestamp', 'timestamp without time zone',
                     'timestamp with time zone', 'timestamptz')}
        for name in names:
            stats = table.get(stats_key, {}).get(name, {})
            add(stats.get('min'))
            add(stats.get('max'))
        for row in table.get('sample_rows', []):
            if isinstance(row, dict):
                for name in names:
                    add(row.get(name))

    for ds in model.get('_datasources', []):
        for table in ds.get('tables', []):
            collect(table, 'hyper_column_stats')
    for hf in hyper_files:
        for table in hf.get('hyper_reader_tables', []) or hf.get('tables', []):
            collect(table, 'column_stats')
    return years


def _calendar_candidates(model, usage):
    """Choose one physical date per table for both bounds and relationships."""
    chosen = []
    for table in model['model']['tables']:
        if table.get('name') == 'Calendar':
            continue
        candidates = [
            (tuple(usage.get((table['name'], col['name']), (0, 0, 0))),
             _date_column_score(col['name']), col['name'])
            for col in table.get('columns', [])
            if col.get('name') and not col.get('isCalculated', False)
            and (col.get('dataType') == 'DateTime' or col.get('dataCategory') == 'DateTime')
        ]
        if candidates:
            chosen.append((table['name'], max(
                candidates, key=lambda item: (item[0], item[1], item[2].casefold()))[2]))
    return chosen


def _runtime_calendar_dates(model, candidates):
    """Evaluate independent source copies through private shared M expressions.

    Do not introduce references to fact queries. Conservatively decline source
    expressions referencing semantic tables, or shared expressions that could
    reference Calendar. This can duplicate source reads, but cannot introduce a
    Calendar -> fact -> Calendar dependency. Each copied source keeps its own
    lexical scope, unchanged. In particular, never rename nested let bindings:
    identical step names in different sources are not collisions (and record
    option names such as Csv.Document's Delimiter are not bindings at all).
    """
    tables = {t['name']: t for t in model['model']['tables']}

    def references(expression, names):
        if isinstance(expression, list):
            expression = '\n'.join(expression)
        # Retain quoted identifiers, remove string literals (e.g. source paths).
        code = re.sub(r'#"(?:[^"]|"")*"|"(?:[^"]|"")*"',
                      lambda match: match.group(0) if match.group(0).startswith('#') else '',
                      expression)
        return any(re.search(r'(?<![\w])(?:#"' + re.escape(name.replace('"', '""'))
                             + r'"|' + re.escape(name) + r')(?![\w])', code)
                   for name in names)

    for expression in model['model'].get('expressions', []):
        if references(expression.get('expression', ''), ['Calendar']):
            return ''
    steps = []
    variables = []
    shared = model['model'].setdefault('expressions', [])
    reserved = {e.get('name', '').casefold() for e in shared}
    reserved.update(name.casefold() for name in tables)
    reserved.update({'datafolder', 'servername', 'databasename', 'rangestart', 'rangeend'})
    for tname, cname in candidates:
        partitions = tables[tname].get('partitions', [])
        if len(partitions) != 1:
            continue
        partition = partitions[0]
        source = partition.get('source', {})
        expression = source.get('expression', '')
        if isinstance(expression, list):
            expression = '\n'.join(expression)
        if (source.get('type') != 'm' or partition.get('mode', 'import') != 'import'
                or not expression or references(expression, list(tables) + ['Calendar'])):
            continue
        identifier = '#"' + (tname + '.' + cname + ' Dates').replace('"', '""') + '"'
        column = cname.replace('"', '""')
        query_name = f'Calendar Source {tname}.{cname}'
        base_name = query_name
        suffix = 2
        while query_name.casefold() in reserved:
            query_name = f'{base_name} {suffix}'
            suffix += 1
        reserved.add(query_name.casefold())
        shared.append({'name': query_name, 'kind': 'm', 'expression': expression,
                       '_calendar_source_table': tname})
        query_ref = '#"' + query_name.replace('"', '""') + '"'
        steps.append(
            f'    {identifier} = List.RemoveNulls(List.Transform(\n'
            f'        Table.Column({query_ref}, "{column}"),\n'
            '        each Date.From(_))),\n')
        variables.append(identifier)
    if not variables:
        return ''
    return ''.join(steps) + '    SourceDates = List.Combine({' + ', '.join(variables) + '}),\n'


def _refresh_calendar_source_expressions(model):
    """Keep private Calendar source copies aligned with final fact partitions.

    Source staging, fail-closed source checks and partition healing may update
    a fact after Calendar creation. Copy that final expression verbatim, without
    treating nested bindings or record fields as a global identifier namespace.
    """
    tables = {t['name']: t for t in model.get('model', {}).get('tables', [])}
    for expression in model.get('model', {}).get('expressions', []):
        tname = expression.get('_calendar_source_table')
        if not tname:
            continue
        partitions = tables.get(tname, {}).get('partitions', [])
        if len(partitions) != 1 or partitions[0].get('source', {}).get('type') != 'm':
            raise ValueError(f"Calendar source '{tname}' no longer has one M partition")
        expression['expression'] = partitions[0]['source']['expression']


def _add_date_table(model, worksheets=None, hyper_files=None, table_rename_map=None):
    """
    Add an automatic date table using Power Query M.

    Uses an M partition (not DAX calculated) to avoid "invalid column ID"
    errors when TMDL relationships reference columns inside
    calculated-table partitions.

    Links Calendar to all fact tables that have physical date columns,
    preferring dates supported by worksheet axes over name heuristics.

    Supports customizable date range via model['_calendar_start'] and
    model['_calendar_end']. Unspecified bounds default to 2020–2030, widened
    to include typed source statistics/embedded dates when available. Without
    that metadata, safe import M sources are evaluated inline for date bounds.

    Skipped if the model already contains a table named 'Calendar'.
    """
    # Guard: don't add if Calendar already exists (e.g. from source data)
    existing_names = {t.get('name', '') for t in model['model']['tables']}
    if 'Calendar' in existing_names:
        return
    source_years = _source_date_years(model, hyper_files or [])
    cal_start = model.get('_calendar_start')
    cal_end = model.get('_calendar_end')
    if cal_start is None:
        cal_start = min({2020} | source_years)
    if cal_end is None:
        cal_end = max({2030} | source_years)
    usage = _worksheet_date_usage(model, worksheets or [], table_rename_map or {})
    cal_candidates = _calendar_candidates(model, usage)
    runtime_dates = ''
    if model.get('_calendar_start') is None or model.get('_calendar_end') is None:
        runtime_dates = _runtime_calendar_dates(model, cal_candidates)
    start_expr = f'#date({cal_start}, 1, 1)'
    end_expr = f'#date({cal_end}, 12, 31)'
    if runtime_dates:
        if model.get('_calendar_start') is None:
            start_expr = f'Date.StartOfYear(List.Min(List.Combine({{SourceDates, {{{start_expr}}}}})))'
        if model.get('_calendar_end') is None:
            end_expr = f'Date.EndOfYear(List.Max(List.Combine({{SourceDates, {{{end_expr}}}}})))'
    cal_culture = model.get('model', {}).get('culture', 'en-US')

    calendar_m = (
        'let\n'
        + runtime_dates +
        f'    StartDate = {start_expr},\n'
        f'    EndDate = {end_expr},\n'
        '    DayCount = List.Max({0, Duration.Days(EndDate - StartDate) + 1}),\n'
        '    DateList = List.Dates(StartDate, DayCount, #duration(1, 0, 0, 0)),\n'
        '    #"Date Table" = Table.FromList(DateList, Splitter.SplitByNothing(), {"Date"}, null, ExtraValues.Error),\n'
        '    #"Changed Type" = Table.TransformColumnTypes(#"Date Table", {{"Date", type date}}),\n'
        '    #"Added Year" = Table.AddColumn(#"Changed Type", "Year", each Date.Year([Date]), Int64.Type),\n'
        '    #"Added Quarter" = Table.AddColumn(#"Added Year", "Quarter", each "Q" & Text.From(Date.QuarterOfYear([Date]))),\n'
        '    #"Added Month" = Table.AddColumn(#"Added Quarter", "Month", each Date.Month([Date]), Int64.Type),\n'
        f'    #"Added MonthName" = Table.AddColumn(#"Added Month", "MonthName", each Date.MonthName([Date], "{cal_culture}")),\n'
        '    #"Added Day" = Table.AddColumn(#"Added MonthName", "Day", each Date.Day([Date]), Int64.Type),\n'
        '    #"Added DayOfWeek" = Table.AddColumn(#"Added Day", "DayOfWeek", each Date.DayOfWeek([Date], Day.Monday) + 1, Int64.Type),\n'
        f'    #"Added DayName" = Table.AddColumn(#"Added DayOfWeek", "DayName", each Date.DayOfWeekName([Date], "{cal_culture}"))\n'
        'in\n'
        '    #"Added DayName"'
    )

    date_table = {
        "name": "Calendar",
        "isHidden": False,
        "columns": [
            {
                "name": "Date",
                "dataType": "DateTime",
                "isKey": True,
                "dataCategory": "DateTime",
                "formatString": "dd/mm/yyyy",
                "sourceColumn": "Date",
                "summarizeBy": "none"
            },
            {
                "name": "Year",
                "dataType": "int64",
                "dataCategory": "Years",
                "sourceColumn": "Year",
                "summarizeBy": "none"
            },
            {
                "name": "Quarter",
                "dataType": "string",
                "sourceColumn": "Quarter",
                "summarizeBy": "none"
            },
            {
                "name": "Month",
                "dataType": "int64",
                "dataCategory": "Months",
                "sourceColumn": "Month",
                "summarizeBy": "none"
            },
            {
                "name": "MonthName",
                "dataType": "string",
                "sourceColumn": "MonthName",
                "sortByColumn": "Month",
                "summarizeBy": "none"
            },
            {
                "name": "Day",
                "dataType": "int64",
                "dataCategory": "Days",
                "sourceColumn": "Day",
                "summarizeBy": "none"
            },
            {
                "name": "DayOfWeek",
                "dataType": "int64",
                "sourceColumn": "DayOfWeek",
                "summarizeBy": "none"
            },
            {
                "name": "DayName",
                "dataType": "string",
                "sourceColumn": "DayName",
                "sortByColumn": "DayOfWeek",
                "summarizeBy": "none"
            }
        ],
        "partitions": [
            {
                "name": "Calendar-Partition",
                "mode": "import",
                "source": {
                    "type": "m",
                    "expression": calendar_m
                }
            }
        ],
        "measures": []
    }

    value_expr = None
    # Find a SUM-based measure in any table for time intelligence
    for t in model["model"]["tables"]:
        if t["name"] == "Calendar":
            continue
        for ms in t.get("measures", []):
            expr = ms.get("expression", "")
            if re.match(r'^SUM\b', expr, re.IGNORECASE):
                value_expr = f'[{ms["name"]}]'
                break
        if value_expr:
            break

    time_intelligence_measures = []
    if value_expr:
        time_intelligence_measures = [
            {
                "name": "Year To Date",
                "expression": f"TOTALYTD({value_expr}, 'Calendar'[Date])",
                "formatString": "#,0.00",
                "displayFolder": "Time Intelligence"
            },
            {
                "name": "Previous Year",
                "expression": f"CALCULATE({value_expr}, SAMEPERIODLASTYEAR('Calendar'[Date]))",
                "formatString": "#,0.00",
                "displayFolder": "Time Intelligence"
            },
            {
                "name": "Year Over Year %",
                "expression": "DIVIDE([Year To Date] - [Previous Year], [Previous Year], 0)",
                "formatString": "0.00%",
                "displayFolder": "Time Intelligence"
            }
        ]

    date_table["measures"].extend(time_intelligence_measures)

    # Add Date hierarchy (Year → Quarter → Month → Day)
    date_table["hierarchies"] = [
        {
            "name": "Date Hierarchy",
            "levels": [
                {"name": "Year", "column": "Year", "ordinal": 0},
                {"name": "Quarter", "column": "Quarter", "ordinal": 1},
                {"name": "Month", "column": "MonthName", "ordinal": 2},
                {"name": "Day", "column": "Day", "ordinal": 3},
            ]
        }
    ]

    model["model"]["tables"].append(date_table)

    # When multiple tables connect to Calendar, use bothDirections so
    # Calendar acts as a shared dimension that bridges cross-table
    # filtering (star schema pattern).  This prevents
    # InvalidUnconstrainedJoin errors in multi-datasource workbooks.
    # A previous/candidate logical alias is not a second fact at the current
    # date grain. An automatic Calendar bridge would silently filter its
    # candidate bag and thereby change a compiled range-predicate SUM.
    candidate_aliases = {pair[1] for pair in model.get('_logical_predicate_pairs', [])
                         if len(pair) == 2}
    cal_candidates = [(table, column) for table, column in cal_candidates
                      if table not in candidate_aliases]
    cross_dir = "bothDirections" if len(cal_candidates) > 1 else "oneDirection"
    for tname, date_col_name in cal_candidates:
        model["model"]["relationships"].append({
            "name": f"Calendar_{tname}_{date_col_name}",
            "fromTable": tname,
            "fromColumn": date_col_name,
            "toTable": "Calendar",
            "toColumn": "Date",
            "crossFilteringBehavior": cross_dir
        })
