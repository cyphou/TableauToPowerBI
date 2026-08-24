"""Prepare local Tableau file sources for Fabric Notebook ingestion."""

from __future__ import annotations

import csv
import os
import re
import shutil
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .fabric_naming import sanitize_table_name
from .fabric_sources import is_file_connection, table_connection


_MAIN_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_DOC_REL_NS = (
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships')
_PKG_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'


def stage_fabric_file_sources(project_dir, datasources):
    """Create canonical ``Data/<table>.csv`` files for Notebook ingestion.

    Existing CSV extracts are matched by table name first, then by their
    header schema. Excel worksheets are converted directly through OOXML.
    Returns project-relative paths of all canonical CSV files.
    """
    data_dir = Path(project_dir) / 'Data'
    data_dir.mkdir(parents=True, exist_ok=True)
    file_tables = []
    for datasource in datasources:
        for table in datasource.get('tables', []):
            connection = table_connection(datasource, table)
            if is_file_connection(connection):
                file_tables.append((table, connection))
    if not file_tables:
        return []

    csv_sources = _load_csv_sources(data_dir)
    workbooks = _load_excel_workbooks(data_dir)
    staged = []
    for table, connection in file_tables:
        table_name = table.get('name', '')
        canonical_name = f'{sanitize_table_name(table_name)}.csv'
        target = data_dir / canonical_name
        expected_headers = [
            column.get('name', '') for column in table.get('columns', [])
            if column.get('name')
        ]
        source_type = str(connection.get('type', '')).strip().lower()
        if source_type == 'excel':
            rows = _find_excel_rows(
                workbooks, table, connection, expected_headers)
            if rows is not None:
                _write_csv(target, rows)
            else:
                source = _find_csv_source(
                    csv_sources, table, connection, expected_headers)
                if source is None:
                    raise FileNotFoundError(
                        f'No staged Excel sheet or converted extract matches '
                        f'Fabric table {table_name!r}')
                _copy_canonical_csv(source['path'], target)
        else:
            source = _find_csv_source(
                csv_sources, table, connection, expected_headers)
            if source is None:
                raise FileNotFoundError(
                    f'No staged CSV matches Fabric table {table_name!r}')
            _copy_canonical_csv(source['path'], target)
        staged.append(target.relative_to(project_dir).as_posix())
    return staged


def read_xlsx_sheets(path):
    """Return ``{sheet_name: rows}`` from an XLSX using standard OOXML."""
    with zipfile.ZipFile(path) as workbook:
        shared_strings = _read_shared_strings(workbook)
        relationships = _read_workbook_relationships(workbook)
        root = ET.fromstring(workbook.read('xl/workbook.xml'))
        sheets = {}
        for sheet in root.findall(f'.//{{{_MAIN_NS}}}sheet'):
            name = sheet.get('name', '')
            relationship_id = sheet.get(f'{{{_DOC_REL_NS}}}id', '')
            target = relationships.get(relationship_id)
            if not name or not target:
                continue
            worksheet_path = _worksheet_path(target)
            rows = _read_worksheet(workbook, worksheet_path, shared_strings)
            sheets[name] = rows
        return sheets


def _load_csv_sources(data_dir):
    sources = []
    for path in data_dir.rglob('*'):
        if not path.is_file() or path.suffix.lower() != '.csv':
            continue
        try:
            with path.open('r', newline='', encoding='utf-8-sig') as stream:
                headers = next(csv.reader(stream), [])
        except (OSError, UnicodeError):
            continue
        sources.append({
            'path': path,
            'stem': path.stem.casefold(),
            'headers': headers,
        })
    return sources


def _load_excel_workbooks(data_dir):
    workbooks = []
    for path in data_dir.rglob('*'):
        if not path.is_file() or path.suffix.lower() != '.xlsx':
            continue
        workbooks.append({
            'path': path,
            'filename': path.name.casefold(),
            'sheets': read_xlsx_sheets(path),
        })
    return workbooks


def _find_csv_source(sources, table, connection, expected_headers):
    details = connection.get('details', {})
    filename = os.path.basename(str(details.get('filename', ''))).casefold()
    names = {
        sanitize_table_name(table.get('name', '')).casefold(),
        _normalize_sheet_name(table.get('source_table', '')).casefold(),
        Path(filename).stem.casefold() if filename else '',
    }
    for source in sources:
        if source['stem'] in names:
            return source
    expected = {_normalize_header(value) for value in expected_headers}
    if expected:
        matching = [
            source for source in sources
            if expected.issubset({
                _normalize_header(value) for value in source['headers']})
        ]
        if len(matching) == 1:
            return matching[0]
    return None


def _find_excel_rows(workbooks, table, connection, expected_headers):
    details = connection.get('details', {})
    filename = os.path.basename(str(details.get('filename', ''))).casefold()
    candidates = [
        _normalize_sheet_name(table.get('source_table', '')),
        _normalize_sheet_name(table.get('name', '')),
    ]
    selected = [
        workbook for workbook in workbooks
        if not filename or workbook['filename'] == filename
    ]
    for workbook in selected:
        by_name = {
            _normalize_sheet_name(name).casefold(): rows
            for name, rows in workbook['sheets'].items()
        }
        for candidate in candidates:
            rows = by_name.get(candidate.casefold())
            if rows is not None:
                return rows
    expected = {_normalize_header(value) for value in expected_headers}
    matches = []
    for workbook in selected:
        for rows in workbook['sheets'].values():
            headers = rows[0] if rows else []
            if expected and expected.issubset({
                    _normalize_header(value) for value in headers}):
                matches.append(rows)
    return matches[0] if len(matches) == 1 else None


def _read_shared_strings(workbook):
    try:
        root = ET.fromstring(workbook.read('xl/sharedStrings.xml'))
    except KeyError:
        return []
    return [
        ''.join(node.text or '' for node in item.iter(f'{{{_MAIN_NS}}}t'))
        for item in root.findall(f'{{{_MAIN_NS}}}si')
    ]


def _read_workbook_relationships(workbook):
    root = ET.fromstring(workbook.read('xl/_rels/workbook.xml.rels'))
    return {
        relationship.get('Id', ''): relationship.get('Target', '')
        for relationship in root.findall(f'{{{_PKG_REL_NS}}}Relationship')
    }


def _worksheet_path(target):
    target = target.replace('\\', '/').lstrip('/')
    if target.startswith('xl/'):
        return target
    while target.startswith('../'):
        target = target[3:]
    return f'xl/{target}'


def _read_worksheet(workbook, worksheet_path, shared_strings):
    root = ET.fromstring(workbook.read(worksheet_path))
    parsed_rows = []
    max_column = 0
    for row in root.findall(f'.//{{{_MAIN_NS}}}row'):
        values = {}
        for cell in row.findall(f'{{{_MAIN_NS}}}c'):
            column = _column_index(cell.get('r', 'A1'))
            values[column] = _cell_value(cell, shared_strings)
            max_column = max(max_column, column)
        parsed_rows.append(values)
    return [
        [values.get(column, '') for column in range(max_column + 1)]
        for values in parsed_rows
    ]


def _cell_value(cell, shared_strings):
    cell_type = cell.get('t', '')
    if cell_type == 'inlineStr':
        return ''.join(
            node.text or '' for node in cell.iter(f'{{{_MAIN_NS}}}t'))
    value_node = cell.find(f'{{{_MAIN_NS}}}v')
    value = value_node.text if value_node is not None else ''
    if cell_type == 's' and value:
        return shared_strings[int(value)]
    if cell_type == 'b':
        return value == '1'
    if cell_type in {'str', 'e'}:
        return value
    if not value:
        return ''
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _column_index(reference):
    match = re.match(r'([A-Za-z]+)', reference)
    letters = match.group(1).upper() if match else 'A'
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - ord('A') + 1
    return index - 1


def _normalize_sheet_name(value):
    value = str(value or '').strip().strip('[]').strip("'")
    if '].[' in value:
        value = value.rsplit('].[', 1)[-1]
    return value.rstrip('$').strip('[]').strip("'")


def _normalize_header(value):
    return re.sub(r'\s+', ' ', str(value or '').strip()).casefold()


def _write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', newline='', encoding='utf-8') as stream:
        csv.writer(stream).writerows(rows)


def _copy_canonical_csv(source, target):
    source = Path(source)
    if source.resolve() == target.resolve() and source.name == target.name:
        return
    if os.path.normcase(str(source.resolve())) == os.path.normcase(
            str(target.resolve())):
        temporary = target.with_name(f'.{target.name}.staging')
        shutil.copyfile(source, temporary)
        os.replace(temporary, target)
        return
    shutil.copyfile(source, target)