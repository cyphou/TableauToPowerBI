"""Tests for deterministic local file staging into Fabric Lakehouse Files."""

import csv
import os
import zipfile

import pytest

from powerbi_import.fabric_file_staging import (
    _copy_canonical_csv,
    read_xlsx_sheets,
    stage_fabric_file_sources,
)


def _write_minimal_xlsx(path):
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships"><sheets>'
        '<sheet name="Orders" sheetId="1" r:id="rId1"/>'
        '</sheets></workbook>')
    relationships = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
        '2006/relationships"><Relationship Id="rId1" '
        'Target="worksheets/sheet1.xml" Type="worksheet"/>'
        '</Relationships>')
    shared = (
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<si><t>Order ID</t></si><si><t>Customer</t></si>'
        '<si><t>Ada</t></si></sst>')
    worksheet = (
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c>'
        '<c r="C1" t="s"><v>1</v></c></row>'
        '<row r="2"><c r="A2"><v>42</v></c>'
        '<c r="B2" t="b"><v>1</v></c>'
        '<c r="C2" t="s"><v>2</v></c></row>'
        '</sheetData></worksheet>')
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', workbook)
        archive.writestr('xl/_rels/workbook.xml.rels', relationships)
        archive.writestr('xl/sharedStrings.xml', shared)
        archive.writestr('xl/worksheets/sheet1.xml', worksheet)


def test_reads_xlsx_shared_strings_booleans_numbers_and_sparse_cells(tmp_path):
    workbook = tmp_path / 'sales.xlsx'
    _write_minimal_xlsx(workbook)

    sheets = read_xlsx_sheets(workbook)

    assert sheets == {
        'Orders': [['Order ID', '', 'Customer'], [42, True, 'Ada']],
    }


def test_converts_referenced_excel_sheet_to_canonical_table_csv(tmp_path):
    project = tmp_path / 'Project'
    data = project / 'Data'
    data.mkdir(parents=True)
    _write_minimal_xlsx(data / 'sales.xlsx')
    datasources = [{
        'connection': {
            'type': 'Excel', 'details': {'filename': 'sales.xlsx'}},
        'tables': [{
            'name': 'Sales Orders',
            'source_table': 'Orders$',
            'columns': [
                {'name': 'Order ID'}, {'name': 'Customer'},
            ],
        }],
    }]

    staged = stage_fabric_file_sources(project, datasources)

    assert staged == ['Data/sales_orders.csv']
    with open(project / staged[0], newline='', encoding='utf-8') as stream:
        assert list(csv.reader(stream)) == [
            ['Order ID', '', 'Customer'], ['42', 'True', 'Ada']]


def test_uses_schema_matched_hyper_csv_for_table_aliases(tmp_path):
    project = tmp_path / 'Project'
    data = project / 'Data'
    data.mkdir(parents=True)
    source = data / 'Extract.csv'
    source.write_text('Order ID,Sales\n1,10.5\n', encoding='utf-8')
    datasources = [{
        'connection': {'type': 'Excel', 'details': {'filename': ''}},
        'tables': [
            {
                'name': 'Orders', 'source_table': 'Orders$',
                'columns': [{'name': 'Order ID'}, {'name': 'Sales'}],
            },
            {
                'name': 'Extract', 'source_table': 'Extract].[Extract',
                'columns': [{'name': 'Order ID'}, {'name': 'Sales'}],
            },
        ],
    }]

    staged = stage_fabric_file_sources(project, datasources)

    assert staged == ['Data/orders.csv', 'Data/extract.csv']
    assert (data / 'orders.csv').read_text(encoding='utf-8') == (
        'Order ID,Sales\n1,10.5\n')
    assert (data / 'extract.csv').is_file()


def test_canonical_copy_accepts_another_name_of_the_same_file(tmp_path):
    # On macOS Extract.csv and extract.csv are one inode while posixpath keeps
    # their case apart; a hard link reproduces that identity on every platform.
    content = 'Order ID,Sales\n1,10.5\n'
    source = tmp_path / 'Extract.csv'
    source.write_text(content, encoding='utf-8')
    target = tmp_path / 'orders.csv'
    try:
        os.link(source, target)
    except (AttributeError, NotImplementedError, OSError) as error:
        pytest.skip(f'hard links are unavailable: {error}')

    _copy_canonical_csv(source, target)

    assert source.read_text(encoding='utf-8') == content
    assert target.read_text(encoding='utf-8') == content
    assert not os.path.samefile(source, target)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        'Extract.csv', 'orders.csv']


def test_canonical_copy_leaves_the_file_alone_when_the_name_matches(tmp_path):
    source = tmp_path / 'orders.csv'
    source.write_text('a,b\n1,2\n', encoding='utf-8')
    before = source.stat().st_mtime_ns

    _copy_canonical_csv(source, source)

    assert source.stat().st_mtime_ns == before
    assert [path.name for path in tmp_path.iterdir()] == ['orders.csv']
