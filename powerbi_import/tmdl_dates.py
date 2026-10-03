"""Date/calendar dimension detection and the generated Calendar table.

A source that ships its own date dimension suppresses the generated Calendar,
so detection and generation belong together: ``_is_date_table`` decides, and
``_add_date_table`` builds the M-partitioned Calendar plus its relationship.
Extracted from ``tmdl_generator``, which re-exports these names.
"""

import re


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


def _add_date_table(model):
    """
    Add an automatic date table using Power Query M.

    Uses an M partition (not DAX calculated) to avoid "invalid column ID"
    errors when TMDL relationships reference columns inside
    calculated-table partitions.

    Links Calendar to ALL fact tables that have date columns
    (not just the first one).

    Supports customizable date range via model['_calendar_start'] and
    model['_calendar_end'] (default: 2020–2030).

    Skipped if the model already contains a table named 'Calendar'.
    """
    # Guard: don't add if Calendar already exists (e.g. from source data)
    existing_names = {t.get('name', '') for t in model['model']['tables']}
    if 'Calendar' in existing_names:
        return
    cal_start = model.get('_calendar_start') or 2020
    cal_end = model.get('_calendar_end') or 2030
    cal_culture = model.get('model', {}).get('culture', 'en-US')

    calendar_m = (
        'let\n'
        f'    StartDate = #date({cal_start}, 1, 1),\n'
        f'    EndDate = #date({cal_end}, 12, 31),\n'
        '    DayCount = Duration.Days(EndDate - StartDate) + 1,\n'
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

    # Add relationships: Calendar[Date] -> each table's first date column
    cal_candidates = []
    for t in model["model"]["tables"]:
        tname = t.get("name", "")
        if tname == "Calendar":
            continue
        candidates = []
        for col in t.get("columns", []):
            if col.get("dataType") == "DateTime" or col.get("dataCategory") == "DateTime":
                date_col_name = col.get("name", "")
                if date_col_name and not col.get("isCalculated", False):
                    candidates.append((
                        _date_column_score(date_col_name),
                        date_col_name,
                    ))
        if candidates:
            _score, date_col_name = max(
                candidates, key=lambda item: (item[0], item[1].casefold()))
            cal_candidates.append((tname, date_col_name))

    # When multiple tables connect to Calendar, use bothDirections so
    # Calendar acts as a shared dimension that bridges cross-table
    # filtering (star schema pattern).  This prevents
    # InvalidUnconstrainedJoin errors in multi-datasource workbooks.
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
