"""Tests for deterministic local file staging into Fabric Lakehouse Files."""

import csv
import os
import zipfile
from xml.sax.saxutils import escape

from powerbi_import.fabric_file_staging import (
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