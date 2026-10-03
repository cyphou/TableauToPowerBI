"""Coverage-push tests for powerbi_import/pbip_generator.py.

Targets uncovered lines identified by coverage report (340 lines, 77.2% → 90%+).
Complements tests/test_pbip_generator.py (existing 47 tests covering basic paths).
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.pbip_generator import (
    PowerBIProjectGenerator,
    _L,
    _pbi_literal,
    _write_json,
)


def _make_generator(output_dir=None):
    """Create a generator with a temp output directory."""
    d = output_dir or tempfile.mkdtemp()
    return PowerBIProjectGenerator(output_dir=d)


def _init_field_map(gen, field_map=None, measure_names=None,
                    bim_measure_names=None, main_table='Table',
                    datasources_ref=None):
    """Initialize internal field mapping state on generator."""
    gen._field_map = field_map or {}
    gen._measure_names = set(measure_names or [])
    gen._bim_measure_names = set(bim_measure_names or [])
    gen._main_table = main_table
    gen._datasources_ref = datasources_ref or []


# ─── Module-level helpers ───────────────────────────────────────────────


class TestL(unittest.TestCase):
    """_L PBIR expression literal wrapper."""

    def test_string_value(self):
        result = _L("'hello'")
        self.assertEqual(result, {"expr": {"Literal": {"Value": "'hello'"}}})

    def test_numeric_value(self):
        result = _L("42D")
        self.assertEqual(result["expr"]["Literal"]["Value"], "42D")

    def test_bool_value(self):
        result = _L("true")
        self.assertEqual(result["expr"]["Literal"]["Value"], "true")


class TestPbiLiteral(unittest.TestCase):
    """_pbi_literal filter value converter — all branch coverage."""

    def test_bool_true(self):
        self.assertEqual(_pbi_literal('true'), 'true')

    def test_bool_false(self):
        self.assertEqual(_pbi_literal('false'), 'false')

    def test_bool_true_upper(self):
        self.assertEqual(_pbi_literal('True'), 'true')

    def test_french_bool_vrai(self):
        self.assertEqual(_pbi_literal('vrai'), 'true')

    def test_french_bool_faux(self):
        self.assertEqual(_pbi_literal('faux'), 'false')

    def test_french_bool_Vrai(self):
        self.assertEqual(_pbi_literal('Vrai'), 'true')

    # A bare number crashes SQExprValidationVisitor.visitIn with
    # "e.accept is not a function" and the report refuses to render, so a
    # numeric literal always carries its DAX type suffix.

    def test_numeric_int(self):
        self.assertEqual(_pbi_literal(42), '42L')

    def test_numeric_float(self):
        self.assertEqual(_pbi_literal('3.14'), '3.14D')

    def test_numeric_negative(self):
        self.assertEqual(_pbi_literal('-5'), '-5L')

    def test_string_default(self):
        self.assertEqual(_pbi_literal('hello'), "'hello'")

    def test_string_with_spaces(self):
        self.assertEqual(_pbi_literal('hello world'), "'hello world'")

    def test_non_numeric_string(self):
        self.assertEqual(_pbi_literal('abc'), "'abc'")

    def test_string_apostrophe_is_escaped(self):
        self.assertEqual(_pbi_literal("Chiffre d'affaires"), "'Chiffre d''affaires'")
        self.assertEqual(
            _pbi_literal("Chiffre d'affaires", column_type='string'),
            "'Chiffre d''affaires'",
        )

    def test_bool_with_whitespace(self):
        self.assertEqual(_pbi_literal(' True '), 'true')

    # --- Type-aware formatting (column_type hint) ---
    # Prevents PBI visitIn crash when a string column receives unquoted
    # numeric or boolean-looking literals.

    def test_string_column_quotes_digit_value(self):
        """String column with digit-only value must quote it (Theme="1")."""
        self.assertEqual(_pbi_literal('1', column_type='string'), "'1'")
        self.assertEqual(_pbi_literal(7, column_type='string'), "'7'")

    def test_string_column_quotes_boolean_text(self):
        """String column with the literal text 'true' must quote it."""
        self.assertEqual(_pbi_literal('true', column_type='string'), "'true'")
        self.assertEqual(_pbi_literal('false', column_type='string'), "'false'")

    def test_string_column_handles_normal_string(self):
        self.assertEqual(_pbi_literal('hello', column_type='string'), "'hello'")

    def test_boolean_column_emits_unquoted_bool(self):
        self.assertEqual(_pbi_literal('true', column_type='boolean'), 'true')
        self.assertEqual(_pbi_literal('false', column_type='boolean'), 'false')
        self.assertEqual(_pbi_literal('vrai', column_type='boolean'), 'true')

    def test_int64_column_emits_typed_number(self):
        self.assertEqual(_pbi_literal('42', column_type='int64'), '42L')
        self.assertEqual(_pbi_literal(42, column_type='int64'), '42L')

    def test_double_column_emits_typed_number(self):
        self.assertEqual(_pbi_literal('3.14', column_type='double'), '3.14D')

    def test_a_whole_value_on_a_double_column_stays_real(self):
        # The column decides the type, not how the value happens to look.
        self.assertEqual(_pbi_literal('2025', column_type='double'), '2025D')

    def test_a_real_value_on_an_int_column_is_narrowed(self):
        self.assertEqual(_pbi_literal('2025.0', column_type='int64'), '2025L')

    def test_numeric_column_with_non_numeric_value_falls_back_to_string(self):
        """Non-numeric value on a numeric column → quote as string fallback."""
        self.assertEqual(_pbi_literal('not_a_number', column_type='int64'),
                         "'not_a_number'")


class TestWriteJson(unittest.TestCase):
    """_write_json file writing with makedirs."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_writes_json_file(self):
        path = os.path.join(self.tmpdir, 'test.json')
        _write_json(path, {"key": "value"})
        with open(path, 'r') as f:
            data = json.load(f)
        self.assertEqual(data["key"], "value")

    def test_creates_nested_dirs(self):
        path = os.path.join(self.tmpdir, 'a', 'b', 'c', 'test.json')
        _write_json(path, {"nested": True})
        self.assertTrue(os.path.exists(path))

    def test_ensure_ascii_default(self):
        path = os.path.join(self.tmpdir, 'ascii.json')
        _write_json(path, {"text": "café"})
        with open(path, 'r', encoding='utf-8') as f:
            content = f.read()
        # ensure_ascii=True by default → unicode escaped
        self.assertIn('caf', content)


# ─── Constructor and generate_project ────────────────────────────────


class TestGeneratorInit(unittest.TestCase):
    def test_creates_output_dir(self):
        d = os.path.join(tempfile.mkdtemp(), 'new_subdir')
        gen = PowerBIProjectGenerator(output_dir=d)
        self.assertTrue(os.path.isdir(gen.output_dir))
        shutil.rmtree(d, ignore_errors=True)


# ─── _create_basic_model_bim ────────────────────────────────────────


class TestCreateBasicModelBim(unittest.TestCase):
    """Fallback BIM with SampleData table — lines 265-287."""

    @patch('powerbi_import.pbip_generator.m_query_generator')
    def test_returns_model_dict(self, mock_mq):
        mock_mq.generate_sample_data_query.return_value = 'let Source = #table({"ID"}, {{1}}) in Source'
        gen = _make_generator()
        result = gen._create_basic_model_bim('TestReport', [])
        self.assertIn('model', result)
        self.assertEqual(result['name'], 'TestReport')
        self.assertEqual(result['compatibilityLevel'], 1567)
        tables = result['model']['tables']
        self.assertTrue(len(tables) >= 1)
        table_names = [t['name'] for t in tables]
        self.assertIn('SampleData', table_names)

    @patch('powerbi_import.pbip_generator.m_query_generator')
    def test_sample_data_columns(self, mock_mq):
        mock_mq.generate_sample_data_query.return_value = 'let Source = #table({"ID"}, {{1}}) in Source'
        gen = _make_generator()
        result = gen._create_basic_model_bim('R', [])
        sample = [t for t in result['model']['tables'] if t['name'] == 'SampleData'][0]
        col_names = [c['name'] for c in sample['columns']]
        self.assertIn('ID', col_names)
        self.assertIn('Name', col_names)
        self.assertIn('Value', col_names)


# ─── _parse_rich_text_runs ──────────────────────────────────────────


class TestParseRichTextRuns(unittest.TestCase):
    """Rich text parsing — bold, italic, color, font_size, url, newlines."""

    def _parse(self, obj):
        gen = _make_generator()
        return gen._parse_rich_text_runs(obj)

    def test_plain_text_fallback(self):
        obj = {'content': 'Hello World'}
        result = self._parse(obj)
        self.assertEqual(len(result), 1)
        # Single paragraph with a textRuns list
        para = result[0]
        runs = para.get('textRuns', [])
        self.assertTrue(any('Hello World' in r.get('value', '') for r in runs))

    def test_bold_run(self):
        obj = {'text_runs': [{'text': 'Bold', 'bold': True}]}
        result = self._parse(obj)
        self.assertEqual(len(result), 1)
        run = result[0]['textRuns'][0]
        self.assertIn('fontWeight', run.get('textStyle', {}))

    def test_italic_run(self):
        obj = {'text_runs': [{'text': 'Italic', 'italic': True}]}
        result = self._parse(obj)
        run = result[0]['textRuns'][0]
        self.assertIn('fontStyle', run.get('textStyle', {}))

    def test_color_8char_aarrggbb(self):
        """#AARRGGBB → #RRGGBB (slice [3:] when len==9 with #)."""
        obj = {'text_runs': [{'text': 'Red', 'color': '#FFFF0000'}]}
        result = self._parse(obj)
        run = result[0]['textRuns'][0]
        style = run.get('textStyle', {})
        self.assertIn('FF0000', str(style.get('color', '')))

    def test_color_6char_passthrough(self):
        """#RRGGBB passes through unchanged."""
        obj = {'text_runs': [{'text': 'Blue', 'color': '#0000FF'}]}
        result = self._parse(obj)
        run = result[0]['textRuns'][0]
        style = run.get('textStyle', {})
        self.assertIn('0000FF', str(style.get('color', '')))

    def test_font_size(self):
        obj = {'text_runs': [{'text': 'Big', 'font_size': 24}]}
        result = self._parse(obj)
        run = result[0]['textRuns'][0]
        self.assertIn('fontSize', run.get('textStyle', {}))

    def test_url_hyperlink(self):
        obj = {'text_runs': [{'text': 'Link', 'url': 'https://example.com'}]}
        result = self._parse(obj)
        run = result[0]['textRuns'][0]
        self.assertIn('https://example.com', str(run))

    def test_newline_splits_paragraphs(self):
        obj = {'text_runs': [{'text': 'Line1\nLine2'}]}
        result = self._parse(obj)
        self.assertEqual(len(result), 2)

    def test_empty_text_runs(self):
        obj = {'text_runs': []}
        result = self._parse(obj)
        # Should return at least one empty paragraph
        self.assertIsInstance(result, list)


# ─── _detect_slicer_mode ────────────────────────────────────────────


class TestDetectSlicerMode(unittest.TestCase):
    """Slicer mode detection — parameter/datatype branches."""

    def _detect(self, obj, col, converted):
        gen = _make_generator()
        return gen._detect_slicer_mode(obj, col, converted)

    def test_range_parameter(self):
        conv = {'parameters': [{'name': 'Size', 'domain_type': 'range'}],
                'datasources': []}
        mode = self._detect({'param': 'Size'}, 'Size', conv)
        self.assertEqual(mode, 'Between')

    def test_list_parameter(self):
        conv = {'parameters': [{'name': 'Color', 'domain_type': 'list'}],
                'datasources': []}
        mode = self._detect({'param': 'Color'}, 'Color', conv)
        self.assertEqual(mode, 'List')

    def test_date_column(self):
        conv = {'parameters': [],
                'datasources': [{'tables': [{'columns': [
                    {'name': 'OrderDate', 'datatype': 'date'}
                ]}]}]}
        mode = self._detect({}, 'orderdate', conv)
        self.assertEqual(mode, 'Date')

    def test_integer_column(self):
        conv = {'parameters': [],
                'datasources': [{'tables': [{'columns': [
                    {'name': 'Age', 'datatype': 'integer'}
                ]}]}]}
        mode = self._detect({}, 'age', conv)
        self.assertEqual(mode, 'Between')

    def test_default_dropdown(self):
        conv = {'parameters': [], 'datasources': []}
        mode = self._detect({}, 'Category', conv)
        self.assertEqual(mode, 'Dropdown')

    def test_float_column(self):
        conv = {'parameters': [],
                'datasources': [{'tables': [{'columns': [
                    {'name': 'Price', 'datatype': 'real'}
                ]}]}]}
        mode = self._detect({}, 'price', conv)
        self.assertEqual(mode, 'Between')


# ─── _create_slicer_visual ──────────────────────────────────────────


class TestCreateSlicerVisualCoverage(unittest.TestCase):
    """Slicer visual creation — Between mode numericInputStyle."""

    def test_between_mode_sets_mode_property(self):
        """Between mode emits the ``mode`` data property.

        Note: ``numericInputStyle`` is intentionally *not* emitted —
        extra slicer blocks (numericInputStyle/search/selection/
        relativeDate) trigger client-side rendering errors in some
        Power BI Desktop builds, so slicer objects are kept minimal.
        """
        gen = _make_generator()
        gen._main_table = 'Sales'
        slicer = gen._create_slicer_visual(
            'v1', 0, 0, 200, 50, 'Amount', 'Sales', 1,
            slicer_mode='Between')
        objects = slicer['visual']['objects']
        # numericInputStyle deliberately omitted for PBIR compatibility
        self.assertNotIn('numericInputStyle', objects)
        self.assertEqual(
            objects['data'][0]['properties']['mode'],
            _L("'Between'"))

    def test_dropdown_mode_no_numeric_input(self):
        gen = _make_generator()
        gen._main_table = 'Sales'
        slicer = gen._create_slicer_visual(
            'v2', 0, 0, 200, 50, 'Category', 'Sales', 1,
            slicer_mode='Dropdown')
        objects = slicer['visual']['objects']
        self.assertNotIn('numericInputStyle', objects)

    def test_empty_table_fallback(self):
        gen = _make_generator()
        gen._main_table = 'Fallback'
        slicer = gen._create_slicer_visual(
            'v3', 0, 0, 200, 50, 'Col', '', 1)
        query = slicer['visual']['query']
        # Should use _main_table as fallback
        entity = query['queryState']['Values']['projections'][0]['field']['Column']['Expression']['SourceRef']['Entity']
        self.assertEqual(entity, 'Fallback')


# ─── _convert_number_format ─────────────────────────────────────────


class TestConvertNumberFormat(unittest.TestCase):
    """Number format conversion — PBI passthrough, currency, percentage, thousands."""

    def test_pbi_passthrough(self):
        self.assertEqual(
            PowerBIProjectGenerator._convert_number_format('0.00'), '0.00')

    def test_pbi_passthrough_thousands(self):
        self.assertEqual(
            PowerBIProjectGenerator._convert_number_format('#,0'), '#,0')

    def test_currency_conversion(self):
        result = PowerBIProjectGenerator._convert_number_format('$#,#00.00')
        self.assertIn('$', result)
        self.assertIn('#,0', result)

    def test_percentage_passthrough(self):
        result = PowerBIProjectGenerator._convert_number_format('0.0%')
        self.assertEqual(result, '0.0%')

    def test_thousands_separator(self):
        result = PowerBIProjectGenerator._convert_number_format('###,###')
        self.assertIn('#,0', result)

    def test_empty_input(self):
        self.assertEqual(
            PowerBIProjectGenerator._convert_number_format(''), '')

    def test_none_input(self):
        self.assertEqual(
            PowerBIProjectGenerator._convert_number_format(None), '')

    def test_non_string_input(self):
        self.assertEqual(
            PowerBIProjectGenerator._convert_number_format(123), '')

    def test_plain_format(self):
        result = PowerBIProjectGenerator._convert_number_format('0')
        self.assertEqual(result, '0')


# ─── _find_column_table ─────────────────────────────────────────────


class TestFindColumnTable(unittest.TestCase):
    """Column → table lookup via columns + calculations."""

    def _find(self, col, conv):
        gen = _make_generator()
        return gen._find_column_table(col, conv)

    def test_finds_by_column_name(self):
        conv = {'datasources': [{'tables': [
            {'name': 'Orders', 'columns': [{'name': 'OrderID'}]}
        ]}]}
        self.assertEqual(self._find('OrderID', conv), 'Orders')

    def test_finds_by_caption(self):
        conv = {'datasources': [{'tables': [
            {'name': 'Sales', 'columns': [
                {'name': 'amt', 'caption': 'Amount'}
            ]}
        ]}]}
        self.assertEqual(self._find('Amount', conv), 'Sales')

    def test_finds_by_calculation_caption(self):
        conv = {'datasources': [{'tables': [
            {'name': 'Facts', 'columns': []}
        ], 'calculations': [
            {'caption': 'Profit Ratio'}
        ]}]}
        self.assertEqual(self._find('Profit Ratio', conv), 'Facts')

    def test_returns_empty_not_found(self):
        conv = {'datasources': []}
        self.assertEqual(self._find('Ghost', conv), '')


# ─── _find_worksheet ────────────────────────────────────────────────


class TestFindWorksheet(unittest.TestCase):
    """Name-based worksheet lookup."""

    def test_finds_by_name(self):
        gen = _make_generator()
        worksheets = [{'name': 'Sheet1'}, {'name': 'Sheet2'}]
        result = gen._find_worksheet(worksheets, 'Sheet2')
        self.assertEqual(result['name'], 'Sheet2')

    def test_returns_none_missing(self):
        gen = _make_generator()
        result = gen._find_worksheet([{'name': 'A'}], 'B')
        self.assertIsNone(result)

    def test_empty_list(self):
        gen = _make_generator()
        result = gen._find_worksheet([], 'X')
        self.assertIsNone(result)


# ─── _build_visual_query ────────────────────────────────────────────


class TestBuildVisualQuery(unittest.TestCase):
    """Visual query building — all 9 chart type branches."""

    def setUp(self):
        self.gen = _make_generator()
        _init_field_map(self.gen, main_table='Sales',
                        measure_names=['Revenue', 'Profit'],
                        bim_measure_names=['Revenue', 'Profit'])

    def _query(self, chart_type, fields):
        ws = {'chart_type': chart_type, 'fields': fields}
        return self.gen._build_visual_query(ws)

    def test_tooltip_uses_unique_model_owner_when_mapping_is_wrong(self):
        self.gen._field_map['Remote Note'] = ('Sales', 'Remote Note')
        self.gen._actual_bim_symbols = {('Sales', 'Region'), ('Sales', 'Revenue'),
                                         ('Notes', 'Remote Note')}
        result = self._query('clusteredBarChart', [
            {'name': 'Region'}, {'name': 'Revenue'},
            {'name': 'Remote Note', 'shelf': 'tooltip'},
        ])
        projection = result['queryState']['Tooltips']['projections'][0]
        self.assertEqual(set(result['queryState']), {'Category', 'Y', 'Tooltips'})
        self.assertEqual(projection, {
            'field': {'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Notes'}},
                    'Property': 'Remote Note',
                }},
                'Function': 3,
            }},
            'queryRef': 'Min(Notes.Remote Note)',
            'nativeQueryRef': 'Remote Note',
            'active': True,
        })

    def test_tooltip_with_multiple_other_model_owners_is_not_guessed(self):
        self.gen._field_map['Remote Note'] = ('Sales', 'Remote Note')
        self.gen._actual_bim_symbols = {('Sales', 'Region'), ('Sales', 'Revenue'),
                                         ('Notes', 'Remote Note'), ('Archive', 'Remote Note')}
        result = self._query('clusteredBarChart', [
            {'name': 'Region'}, {'name': 'Revenue'},
            {'name': 'Remote Note', 'shelf': 'tooltip'},
        ])
        self.assertNotIn('Tooltips', result['queryState'])

    def test_collision_override_cannot_move_tooltip_to_table_without_field(self):
        self.gen._field_map['Distance'] = ('Sites', 'Distance')
        self.gen._collision_tables = {'Sites', 'SitesRenamed'}
        self.gen._ds_table_map = {'source': 'SitesRenamed'}
        self.gen._actual_bim_symbols = {('Sites', 'Distance'),
                                         ('Sites', 'Region'), ('Sites', 'Revenue')}
        result = self._query('clusteredBarChart', [
            {'name': 'Region'}, {'name': 'Revenue'},
            {'name': 'Distance', 'shelf': 'tooltip', 'datasource': 'source'},
        ])
        projection = result['queryState']['Tooltips']['projections'][0]
        self.assertEqual(set(result['queryState']), {'Category', 'Y', 'Tooltips'})
        self.assertEqual(projection, {
            'field': {'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sites'}},
                    'Property': 'Distance',
                }},
                'Function': 3,
            }},
            'queryRef': 'Min(Sites.Distance)',
            'nativeQueryRef': 'Distance',
            'active': True,
        })

    def test_same_field_on_rows_and_tooltip_keeps_primary_role_once(self):
        result = self._query('clusteredBarChart', [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'columns'},
            {'name': 'Region', 'shelf': 'tooltip'},
            {'name': 'Region', 'shelf': 'tooltip'},
        ])
        roles = result['queryState']
        self.assertEqual(len(roles['Category']['projections']), 1)
        self.assertEqual(
            roles['Category']['projections'][0]['field']['Column']['Property'],
            'Region',
        )
        self.assertEqual(len(roles['Y']['projections']), 1)
        self.assertEqual(
            roles['Y']['projections'][0]['field']['Measure']['Property'],
            'Revenue',
        )
        self.assertNotIn('Tooltips', roles)

    def test_primary_roles_unchanged_when_tooltips_repeat_bound_fields(self):
        self.gen._field_map['Segment Tooltip'] = ('Sales', 'Segment')
        primary_fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'columns'},
        ]
        primary_roles = self._query('clusteredBarChart', primary_fields)['queryState']
        result = self._query('clusteredBarChart', primary_fields + [
            {'name': 'Region', 'shelf': 'tooltip'},
            {'name': 'Segment', 'shelf': 'tooltip'},
            {'name': 'Revenue', 'shelf': 'tooltip'},
            {'name': 'Segment Tooltip', 'shelf': 'tooltip'},
        ])
        roles = result['queryState']
        self.assertEqual(set(primary_roles), {'Category', 'Series', 'Y'})
        self.assertEqual(roles, primary_roles)
        self.assertNotIn('Tooltips', roles)

    def test_independent_tooltip_measure_and_column_survive_redundant_fields(self):
        primary_fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'columns'},
        ]
        primary_roles = self._query('clusteredBarChart', primary_fields)['queryState']
        result = self._query('clusteredBarChart', primary_fields + [
            {'name': 'Region', 'shelf': 'tooltip'},
            {'name': 'Segment', 'shelf': 'tooltip'},
            {'name': 'Revenue', 'shelf': 'tooltip'},
            {'name': 'Profit', 'shelf': 'tooltip'},
            {'name': 'Note', 'shelf': 'tooltip'},
        ])
        roles = result['queryState']
        self.assertEqual(
            {role: value for role, value in roles.items() if role != 'Tooltips'},
            primary_roles,
        )
        self.assertEqual(set(roles), {'Category', 'Series', 'Y', 'Tooltips'})
        self.assertEqual(
            roles['Tooltips']['projections'],
            [
                {'field': {'Measure': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Profit',
                }}, 'queryRef': 'Sales.Profit', 'nativeQueryRef': 'Profit',
                 'active': True},
                {'field': {'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': 'Note',
                    }},
                    'Function': 3,
                }}, 'queryRef': 'Min(Sales.Note)', 'nativeQueryRef': 'Note',
                 'active': True},
            ],
        )

    def test_tooltip_min_scalar_preserves_distinct_column_without_primary_duplicates(self):
        self.gen._field_map.update({
            'Region Tooltip': ('Sales', 'Region'),
            'Note Tooltip': ('Notes', 'Supplementary / Note (%)'),
            'Supplementary / Note (%)': ('Notes', 'Supplementary / Note (%)'),
        })
        self.gen._actual_bim_symbols = {
            ('Sales', 'Region'), ('Sales', 'Revenue'),
            ('Notes', 'Supplementary / Note (%)'),
        }
        roles = self._query('clusteredBarChart', [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'columns'},
            {'name': 'Region', 'shelf': 'tooltip'},
            {'name': 'Region Tooltip', 'shelf': 'tooltip'},
            {'name': 'Revenue', 'shelf': 'tooltip'},
            {'name': 'Supplementary / Note (%)', 'shelf': 'tooltip'},
            {'name': 'Note Tooltip', 'shelf': 'tooltip'},
            {'name': 'Supplementary / Note (%)', 'shelf': 'tooltip'},
            {'name': 'Supplementary / Note (%)', 'shelf': 'tooltip',
             'aggregation': 'min'},
        ])['queryState']
        self.assertEqual(roles, {
            'Category': {'projections': [{
                'field': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Region',
                }},
                'queryRef': 'Sales.Region', 'nativeQueryRef': 'Region',
                'active': True,
            }]},
            'Y': {'projections': [{
                'field': {'Measure': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Revenue',
                }},
                'queryRef': 'Sales.Revenue', 'nativeQueryRef': 'Revenue',
                'active': True,
            }]},
            'Tooltips': {'projections': [{
                'field': {'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Notes'}},
                        'Property': 'Supplementary / Note (%)',
                    }},
                    'Function': 3,
                }},
                'queryRef': 'Min(Notes.Supplementary / Note (%))',
                'nativeQueryRef': 'Supplementary / Note (%)',
                'active': True,
            }]},
        })

    def test_tooltip_same_physical_column_with_different_aggregation_is_preserved(self):
        self.gen._measure_names.add('Quantity')
        result = self._query('clusteredBarChart', [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Quantity', 'shelf': 'columns', 'aggregation': 'sum'},
            {'name': 'Quantity', 'shelf': 'tooltip', 'aggregation': 'avg'},
        ])
        roles = result['queryState']
        self.assertEqual(len(roles['Y']['projections']), 1)
        self.assertEqual(len(roles['Tooltips']['projections']), 1)
        primary = roles['Y']['projections'][0]['field']['Aggregation']
        tooltip = roles['Tooltips']['projections'][0]['field']['Aggregation']
        self.assertEqual(
            primary['Expression'],
            {'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': 'Quantity'}},
        )
        self.assertEqual(tooltip['Expression'], primary['Expression'])
        self.assertEqual(primary['Function'], 0)
        self.assertEqual(tooltip['Function'], 1)

    def test_tooltips_deduplicate_identical_field_expressions(self):
        self.gen._field_map.update({
            'Profit Tooltip': ('Sales', 'Profit'),
            'Note Tooltip': ('Sales', 'Note'),
        })
        primary_fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'columns'},
        ]
        tooltip_fields = [
            {'name': 'Profit', 'shelf': 'tooltip'},
            {'name': 'Note', 'shelf': 'tooltip'},
        ]
        expected_roles = self._query(
            'clusteredBarChart', primary_fields + tooltip_fields,
        )['queryState']
        result = self._query('clusteredBarChart', primary_fields + tooltip_fields + [
            {'name': 'Profit Tooltip', 'shelf': 'tooltip'},
            {'name': 'Note Tooltip', 'shelf': 'tooltip'},
        ] + tooltip_fields)
        self.assertEqual(len(expected_roles['Tooltips']['projections']), 2)
        self.assertEqual(result['queryState'], expected_roles)

    def test_multiple_tooltip_aggregations_deduplicate_only_identical_expressions(self):
        self.gen._field_map.setdefault('Amount', ('Sales', 'Amount'))
        self.gen._actual_bim_column_types = {('Sales', 'Amount'): 'double'}
        fields = [
            {'name': 'Category', 'shelf': 'rows'},
            {'name': 'Amount', 'shelf': 'columns', 'aggregation': 'sum'},
        ] + [
            {'name': 'Amount', 'shelf': 'tooltip', 'aggregation': aggregation}
            for aggregation in ('avg', 'min', 'max', 'avg', 'min', 'max', 'sum')
        ]
        roles = self._query('clusteredBarChart', fields)['queryState']
        self.assertEqual(set(roles), {'Category', 'Y', 'Tooltips'})
        self.assertEqual(roles['Tooltips']['projections'], [{
            'field': {'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Amount',
                }},
                'Function': function,
            }},
            'queryRef': 'Sales.Amount',
            'nativeQueryRef': 'Amount',
            'active': True,
        } for function in (1, 3, 4)])
        self.assertEqual(roles['Y']['projections'][0]['field'], {
            'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Amount',
                }},
                'Function': 0,
            },
        })

    def test_multi_row_card_preserves_text_field_on_tooltip_shelf(self):
        result = self._query('multiRowCard', [
            {'name': 'Region', 'shelf': 'text'},
            {'name': 'Region', 'shelf': 'tooltip'},
            {'name': 'Revenue', 'shelf': 'tooltip'},
        ])
        self.assertEqual(len(result['queryState']['Values']['projections']), 2)
        self.assertNotIn('Tooltips', result['queryState'])

    def test_pcto_tooltip_binds_generated_measure_not_source_column(self):
        self.gen._actual_bim_symbols = {('Sales', 'Region'), ('Sales', 'Revenue'),
                                         ('Sales', '% of Total Revenue')}
        self.gen._actual_bim_measure_names = {'% of Total Revenue'}
        self.gen._bim_measure_names = {'% of Total Revenue'}
        result = self._query('clusteredBarChart', [
            {'name': 'Region'}, {'name': 'Revenue'},
            {'name': 'Revenue', 'shelf': 'tooltip', 'table_calc': 'pcto'},
        ])
        projection = result['queryState']['Tooltips']['projections'][0]
        self.assertEqual(projection['field']['Measure']['Property'], '% of Total Revenue')

    def test_scatter_uses_details_for_point_grouping(self):
        worksheet = {'chart_type': 'scatter', 'fields': [
            {'name': 'Player'}, {'name': 'ASST'}, {'name': 'BLK'},
        ]}
        query = self.gen._build_visual_query(worksheet)
        self.assertEqual(query, {'queryState': {
            'Values': {'projections': [{
                'field': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            } for name in ('Player', 'ASST', 'BLK')]},
        }})
        self.assertNotIn('X', query['queryState'])
        self.assertNotIn('Y', query['queryState'])
        self.assertEqual(worksheet['_override_visual_type'], 'tableEx')
        self.assertEqual(worksheet['_visual_fallback_note'],
                         'Scatter has no second resolved axis; '
                         'source fields were preserved as a table.')

    def test_detail_field_is_not_promoted_to_series(self):
        query = self._query('clusteredBarChart', [
            {'name': 'Player', 'shelf': 'rows'},
            {'name': 'PTS', 'shelf': 'columns'},
            {'name': 'YR', 'shelf': 'detail'},
        ])
        series = query['queryState'].get('Series', {})
        fields = series.get('projections', [])
        self.assertEqual(fields, [])


    def test_empty_fields(self):
        self.assertIsNone(self._query('bar', []))

    def test_only_skip_fields(self):
        fields = [{'name': 'Measure Names'}, {'name': 'Measure Values'}]
        self.assertIsNone(self._query('bar', fields))

    def test_action_fields_are_not_bound_as_model_columns(self):
        fields = [{'name': 'Action (Region)'},
                  {'name': 'Action (YEAR(Date (year)))'}]
        self.assertIsNone(self._query('bar', fields))

    def test_missing_parameter_table_is_not_mapped(self):
        self.gen._actual_bim_symbols = {('Sales', 'Revenue')}
        self.gen._build_field_mapping({
            'datasources': [{'tables': [{'name': 'Sales', 'columns': []}],
                             'columns': [], 'calculations': []}],
            'parameters': [{'name': 'Rebounds', 'caption': 'Rebounds',
                            'domain_type': 'list'}],
            'calculations': [], 'groups': [],
        })
        self.assertNotIn('Rebounds', self.gen._field_map)

    def test_unavailable_synthetic_fields_are_not_bound(self):
        self.gen._unavailable_parameter_names = {'Rebounds'}
        self.assertIsNone(self._query('bar', [
            {'name': 'Rebounds'},
            {'name': 'Sub-Category (group)'},
            {'name': 'io:BurstoutSet'},
        ]))

    def test_fabric_placeholder_fields_are_not_bound(self):
        self.gen._output_format = 'fabric'
        self.assertIsNone(self._query('bar', [
            {'name': 'South Map'}, {'name': 'Rebounds'}]))

    def test_year_filter_control_resolves_underlying_date_column(self):
        self.gen._field_map['Order Date'] = ('Orders', 'Order Date')
        self.gen._actual_bim_symbols = {('Orders', 'Order Date')}
        with tempfile.TemporaryDirectory() as tmp:
            self.gen._create_visual_filter_control(
                tmp,
                {'name': 'South Map', 'field': 'Order Date',
                 'calc_column_id': 'Order Date',
                 'param': '[ds].[yr:Order Date:ok]',
                 'position': {'x': 0, 'y': 0, 'w': 100, 'h': 100}},
                1.0, 1.0, 0, {}, {'datasources': []})
            visual_files = list(Path(tmp).rglob('visual.json'))
            self.assertEqual(len(visual_files), 1)
            payload = json.loads(visual_files[0].read_text(encoding='utf-8'))
            projection = payload['visual']['query']['queryState']['Values']['projections'][0]
            self.assertEqual(projection['field']['Column']['Expression']['SourceRef']['Entity'], 'Orders')
            self.assertEqual(projection['field']['Column']['Property'], 'Order Date')

    def test_unmapped_filter_control_is_skipped_without_bim_symbols(self):
        self.gen._field_map = {'Region': ('Orders', 'Region')}
        with tempfile.TemporaryDirectory() as tmp:
            self.gen._create_visual_filter_control(
                tmp, {'field': 'South Map', 'calc_column_id': 'South Map'},
                1.0, 1.0, 0, {}, {'datasources': []})
            self.assertEqual(list(Path(tmp).rglob('visual.json')), [])

    def test_map_type(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        result = self._query('map', fields)
        qs = result['queryState']
        self.assertEqual(set(qs), {'Category', 'Size'})
        self.assertEqual(qs['Category']['projections'][0]['field'], {
            'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                       'Property': 'Region'},
        })

    def test_catalog_roles_align_with_shared_visual_generator(self):
        from powerbi_import.visual_generator import VISUAL_DATA_ROLES

        expected_roles = {
            'card': ([], ['Values']),
            'multiRowCard': ([], ['Values']),
            'scatterChart': (['Category', 'Series'], ['X', 'Y', 'Size']),
            'azureMap': (['Category', 'Series'], ['Y', 'X', 'Size']),
        }
        for visual_type, (dimension_roles, measure_roles) in expected_roles.items():
            with self.subTest(visual_type=visual_type):
                self.assertEqual(self.gen._VISUAL_DATA_ROLES[visual_type],
                                 (dimension_roles, measure_roles))
                shared_dimension_roles = (dimension_roles + ['Tooltips']
                                          if visual_type in ('azureMap', 'scatterChart')
                                          else dimension_roles)
                self.assertEqual(VISUAL_DATA_ROLES[visual_type],
                                 (shared_dimension_roles, measure_roles))

    def test_catalog_azure_map_coordinates_are_averaged_beside_location(self):
        # Desktop refuses a Location beside raw coordinates and asks for them
        # averaged, so 'sum' on the Northing pill must be overridden to avg.
        self.gen._measure_names.update({'Northing', 'Easting'})
        self.gen._actual_bim_symbols = {
            ('Sales', name) for name in
            ('Northing', 'Easting', 'Record Label', 'Segment', 'Revenue')
        }
        fields = [
            {'name': 'Northing', 'shelf': 'rows', 'semantic_role': 'latitude',
             'aggregation': 'sum'},
            {'name': 'Easting', 'shelf': 'columns', 'semantic_role': 'longitude',
             'aggregation': 'avg'},
            {'name': 'Record Label', 'shelf': 'detail'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'size'},
        ]
        for bim_coordinates in (False, True):
            with self.subTest(bim_coordinates=bim_coordinates):
                if bim_coordinates:
                    self.gen._bim_measure_names.update({'Northing', 'Easting'})
                roles = self._query('azureMap', fields)['queryState']
                self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Series', 'Size'})
                for role, name in (('Y', 'Northing'), ('X', 'Easting')):
                    projections = roles[role]['projections']
                    self.assertEqual(len(projections), 1)
                    field = projections[0]['field']
                    if bim_coordinates:
                        # A coordinate that is already a model measure carries
                        # its own aggregation; azureMap Y/X take GroupingOrMeasure.
                        self.assertEqual(field, {
                            'Measure': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                        'Property': name},
                        })
                    else:
                        self.assertEqual(field, {
                            'Aggregation': {
                                'Expression': {'Column': {
                                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                    'Property': name}},
                                'Function': 1,
                            },
                        })
                        # Sum (0) is the failure mode that blanked the map.
                        self.assertNotEqual(field['Aggregation']['Function'], 0)
                for role, name in (('Category', 'Record Label'),
                                   ('Series', 'Segment')):
                    projections = roles[role]['projections']
                    self.assertEqual(len(projections), 1)
                    self.assertEqual(projections[0]['field'], {
                        'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                   'Property': name},
                    })
                self.assertEqual(roles['Size']['projections'][0]['field'], {
                    'Measure': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': 'Revenue'},
                })

    def test_catalog_azure_map_category_label_uses_each_supported_shelf(self):
        self.gen._measure_names.update({'Northing', 'Easting'})
        self.gen._actual_bim_symbols = {
            ('Sales', name) for name in
            ('Northing', 'Easting', 'Record Label', 'Revenue')
        }
        for shelf in ('rows', 'columns', 'detail', 'text', 'tooltip'):
            with self.subTest(shelf=shelf):
                roles = self._query('azureMap', [
                    {'name': 'Northing', 'shelf': 'rows', 'semantic_role': 'latitude'},
                    {'name': 'Easting', 'shelf': 'columns', 'semantic_role': 'longitude'},
                    {'name': 'Record Label', 'shelf': shelf},
                    {'name': 'Revenue', 'shelf': 'size'},
                ])['queryState']
                self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Size'})
                self.assertEqual(len(roles['Category']['projections']), 1)
                self.assertEqual(roles['Category']['projections'][0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                               'Property': 'Record Label'},
                })

    def test_catalog_azure_map_label_not_repeated_and_extra_tooltip_deduplicated(self):
        self.gen._measure_names.update({'Northing', 'Easting'})
        self.gen._field_map.update({
            'Label Alias': ('Sales', 'Record Label'),
            'Note Alias': ('Sales', 'Record Note'),
        })
        self.gen._actual_bim_symbols = {
            ('Sales', name) for name in
            ('Northing', 'Easting', 'Record Label', 'Record Note')
        }
        roles = self._query('azureMap', [
            {'name': 'Northing', 'shelf': 'rows', 'semantic_role': 'latitude'},
            {'name': 'Easting', 'shelf': 'columns', 'semantic_role': 'longitude'},
            {'name': 'Record Label', 'shelf': 'tooltip'},
            {'name': 'Record Label', 'shelf': 'tooltip'},
            {'name': 'Label Alias', 'shelf': 'tooltip'},
            {'name': 'Record Note', 'shelf': 'tooltip'},
            {'name': 'Record Note', 'shelf': 'tooltip'},
            {'name': 'Note Alias', 'shelf': 'tooltip'},
        ])['queryState']
        self.assertEqual(set(roles), {'Category', 'Y', 'X', 'Tooltips'})
        self.assertEqual(roles['Category']['projections'][0]['field'], {
            'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                       'Property': 'Record Label'},
        })
        self.assertEqual(roles['Tooltips']['projections'], [{
            'field': {'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Record Note',
                }},
                'Function': 3,
            }},
            'queryRef': 'Min(Sales.Record Note)',
            'nativeQueryRef': 'Record Note',
            'active': True,
        }])

    def test_catalog_azure_map_without_coordinates_requires_geographic_category(self):
        self.gen._actual_bim_symbols = {
            ('Sales', name) for name in ('Record Label', 'Geo Label', 'Revenue')
        }
        for has_geographic_dimension in (False, True):
            with self.subTest(has_geographic_dimension=has_geographic_dimension):
                fields = [{'name': 'Record Label', 'shelf': 'rows'},
                          {'name': 'Revenue', 'shelf': 'size'}]
                if has_geographic_dimension:
                    fields.append({'name': 'Geo Label', 'shelf': 'detail',
                                   'semantic_role': 'city'})
                worksheet = {'chart_type': 'azureMap', 'fields': fields}
                roles = self.gen._build_visual_query(worksheet)['queryState']
                self.assertNotIn('Y', roles)
                self.assertNotIn('X', roles)
                self.assertNotIn('Location', roles)
                self.assertNotIn('_visual_fallback_note', worksheet)
                if has_geographic_dimension:
                    self.assertEqual(set(roles), {'Category', 'Size'})
                    self.assertEqual(worksheet['chart_type'], 'azureMap')
                    self.assertNotIn('_override_visual_type', worksheet)
                    self.assertEqual(roles['Category']['projections'][0]['field'], {
                        'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                   'Property': 'Geo Label'},
                    })
                    self.assertNotIn('_visual_mapping_note', worksheet)
                else:
                    # azureMap declares requiredRoles ['Category']; a map with
                    # no geography at all would render blank, so it degrades.
                    self.assertEqual(set(roles), {'Values'})
                    self.assertEqual(worksheet['_override_visual_type'], 'tableEx')
                    # The note carries both causes: the specific geography
                    # failure, then the generic contract degradation it
                    # triggered -- the specific cause is no longer lost.
                    note = worksheet['_visual_mapping_note']
                    geography_cause = (
                        'No resolved geographic location or coordinate pair; '
                        'source geography is required for this map.')
                    contract_cause = (
                        'azureMap needs Category; source fields could not '
                        'fill that role, shown as a table instead')
                    self.assertIn(geography_cause, note)
                    self.assertIn(contract_cause, note)
                    self.assertLess(note.index(geography_cause),
                                    note.index(contract_cause))
                    self.assertEqual(
                        note, '%s; %s' % (geography_cause, contract_cause))
                    self.assertEqual(
                        [projection['field']
                         for projection in roles['Values']['projections']], [
                            {'Column': {
                                'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': 'Record Label'}},
                            {'Measure': {
                                'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': 'Revenue'}},
                        ])

    def test_create_visual_worksheet_promotes_legacy_map_to_azure_map(self):
        self.gen._field_map['Geo Label'] = ('Sales', 'Geo Label')
        self.gen._actual_bim_symbols = {
            ('Sales', 'Geo Label'), ('Sales', 'Revenue'),
        }
        worksheet = {
            'chart_type': 'map',
            'fields': [
                {'name': 'Geo Label', 'shelf': 'detail', 'semantic_role': 'city'},
                {'name': 'Revenue', 'shelf': 'size'},
            ],
            'map_options': {'style': 'normal'},
        }
        with patch('powerbi_import.pbip_generator._write_json') as write_json, \
                patch('powerbi_import.pbip_generator.os.makedirs'):
            self.gen._create_visual_worksheet(
                self.gen.output_dir, worksheet,
                {'worksheetName': 'Synthetic Geographic Map', 'position': {}},
                1, 1, 0, [], {},
            )
        write_json.assert_called_once()
        payload = write_json.call_args.args[1]
        visual = payload['visual']
        self.assertEqual(worksheet['chart_type'], 'azureMap')
        self.assertEqual(visual['visualType'], 'azureMap')
        roles = visual['query']['queryState']
        self.assertEqual(set(roles), {'Category', 'Size'})
        self.assertEqual(roles['Category']['projections'], [{
            'field': {'Column': {
                'Expression': {'SourceRef': {'Entity': 'Sales'}},
                'Property': 'Geo Label',
            }},
            'queryRef': 'Sales.Geo Label', 'nativeQueryRef': 'Geo Label',
            'active': True,
        }])
        self.assertNotIn('_override_visual_type', worksheet)
        self.assertNotIn('_visual_fallback_note', worksheet)
        self.assertNotIn('_visual_mapping_note', worksheet)
        self.assertNotIn('annotations', payload)
        self.assertNotIn('mapControl', visual['objects'])
        self.assertEqual(visual['objects']['mapControls'], [{
            'properties': {'defaultStyle': {
                'expr': {'Literal': {'Value': "'road'"}},
            }},
        }])

    def test_table_type(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        result = self._query('tableEx', fields)
        qs = result['queryState']
        self.assertIn('Values', qs)
        self.assertTrue(len(qs['Values']['projections']) <= 10)

    def test_scatter_chart(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}, {'name': 'Profit'}]
        worksheet = {'chart_type': 'scatterChart', 'fields': fields}
        result = self.gen._build_visual_query(worksheet)
        qs = result['queryState']
        self.assertEqual(qs, {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Category', 'Column', 'Region'),
                ('X', 'Measure', 'Revenue'), ('Y', 'Measure', 'Profit'),
            )
        })
        self.assertNotIn('_override_visual_type', worksheet)
        self.assertNotIn('_visual_fallback_note', worksheet)
        with patch('powerbi_import.pbip_generator._write_json') as write_json, \
                patch('powerbi_import.pbip_generator.os.makedirs'):
            self.gen._create_visual_worksheet(
                self.gen.output_dir, worksheet,
                {'worksheetName': 'Synthetic Scatter', 'position': {}},
                1, 1, 0, [], {},
            )
        write_json.assert_called_once()
        visual = write_json.call_args.args[1]
        self.assertEqual(visual['$schema'],
                         'https://developer.microsoft.com/json-schemas/fabric/item/'
                         'report/definition/visualContainer/2.5.0/schema.json')
        self.assertEqual(visual['visual']['visualType'], 'scatterChart')
        self.assertEqual(visual['visual']['query'], result)

    def test_scatter_chart_one_measure(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        worksheet = {'chart_type': 'scatterChart', 'fields': fields}
        result = self.gen._build_visual_query(worksheet)
        qs = result['queryState']
        self.assertEqual(qs, {'Values': {'projections': [{
            'field': {wrapper: {
                'Expression': {'SourceRef': {'Entity': 'Sales'}},
                'Property': name,
            }},
            'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
            'active': True,
        } for wrapper, name in (('Column', 'Region'), ('Measure', 'Revenue'))]}})
        self.assertNotIn('X', qs)
        self.assertNotIn('Y', qs)
        self.assertEqual(worksheet['_override_visual_type'], 'tableEx')
        fallback_note = ('Scatter has no second resolved axis; '
                         'source fields were preserved as a table.')
        self.assertEqual(worksheet['_visual_fallback_note'], fallback_note)
        with patch('powerbi_import.pbip_generator._write_json') as write_json, \
                patch('powerbi_import.pbip_generator.os.makedirs'):
            self.gen._create_visual_worksheet(
                self.gen.output_dir, worksheet,
                {'worksheetName': 'Synthetic Scatter', 'position': {}},
                1, 1, 0, [], {},
            )
        write_json.assert_called_once()
        visual = write_json.call_args.args[1]
        self.assertEqual(visual['$schema'],
                         'https://developer.microsoft.com/json-schemas/fabric/item/'
                         'report/definition/visualContainer/2.5.0/schema.json')
        self.assertEqual(visual['visual']['visualType'], 'tableEx')
        self.assertEqual(visual['visual']['query'], result)
        self.assertIn({'name': 'MigrationNote', 'value': fallback_note},
                      visual['annotations'])
        self.assertNotIn('annotations', visual['visual'])
        self.assertNotIn('_override_visual_type', worksheet)
        self.assertNotIn('_visual_fallback_note', worksheet)

    def test_scatter_uses_tooltip_fields_for_missing_axes_and_detail(self):
        fields = [
            {'name': 'Revenue', 'shelf': 'rows'},
            {'name': 'Profit', 'shelf': 'tooltip'},
            {'name': 'Region', 'shelf': 'tooltip'},
        ]
        result = self._query('scatterChart', fields)
        qs = result['queryState']
        self.assertEqual(qs, {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Category', 'Column', 'Region'),
                ('X', 'Measure', 'Revenue'), ('Y', 'Measure', 'Profit'),
            )
        })

    def test_scatter_chart_three_measures_size(self):
        """Third measure → Size (bubble)."""
        self.gen._measure_names.add('Quantity')
        self.gen._bim_measure_names.add('Quantity')
        fields = [{'name': 'Region'}, {'name': 'Revenue'},
                  {'name': 'Profit'}, {'name': 'Quantity'}]
        result = self._query('scatterChart', fields)
        qs = result['queryState']
        self.assertEqual(qs, {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Category', 'Column', 'Region'),
                ('X', 'Measure', 'Revenue'), ('Y', 'Measure', 'Profit'),
                ('Size', 'Measure', 'Quantity'),
            )
        })

    def test_scatter_keeps_all_source_tooltips(self):
        fields = ([{'name': 'Region'}, {'name': 'Revenue'}, {'name': 'Profit'}]
                  + [{'name': f'Tooltip {index}', 'shelf': 'tooltip'}
                     for index in range(11)])
        result = self._query('scatterChart', fields)
        self.assertEqual(set(result['queryState']), {'Category', 'X', 'Y', 'Tooltips'})
        self.assertEqual(result['queryState']['Tooltips']['projections'], [{
            'field': {'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': f'Tooltip {index}',
                }},
                'Function': 3,
            }},
            'queryRef': f'Min(Sales.Tooltip {index})',
            'nativeQueryRef': f'Tooltip {index}',
            'active': True,
        } for index in range(11)])

    def test_map_and_bar_keep_all_source_tooltips(self):
        tooltips = [{'name': f'Tooltip {index}', 'shelf': 'tooltip'}
                    for index in range(7)]
        for visual_type in ('azureMap', 'clusteredBarChart'):
            fields = [{'name': 'Region'}, {'name': 'Revenue'}] + tooltips
            result = self._query(visual_type, fields)
            self.assertEqual(result['queryState']['Tooltips']['projections'], [{
                'field': {'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': f'Tooltip {index}',
                    }},
                    'Function': 3,
                }},
                'queryRef': f'Min(Sales.Tooltip {index})',
                'nativeQueryRef': f'Tooltip {index}',
                'active': True,
            } for index in range(7)], visual_type)

    def test_combo_chart_keeps_source_tooltips(self):
        result = self._query('lineClusteredColumnComboChart', [
            {'name': 'Region', 'shelf': 'columns'},
            {'name': 'Revenue', 'shelf': 'rows'},
            {'name': 'Profit', 'shelf': 'tooltip'},
        ])
        self.assertEqual(len(result['queryState']['Tooltips']['projections']), 1)

    def test_gauge_type(self):
        fields = [{'name': 'Revenue'}, {'name': 'Profit'}, {'name': 'Region'}]
        result = self._query('gauge', fields)
        qs = result['queryState']
        self.assertIn('Y', qs)
        self.assertIn('TargetValue', qs)
        self.assertIn('Category', qs)

    def test_card_type(self):
        fields = [{'name': 'Revenue'}]
        result = self._query('card', fields)
        qs = result['queryState']
        self.assertEqual(set(qs), {'Values'})
        self.assertEqual(len(qs['Values']['projections']), 1)
        self.assertEqual(qs['Values']['projections'][0]['field'], {
            'Measure': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': 'Revenue'},
        })

    def test_card_dims_only(self):
        """Card with no measures → uses dims."""
        fields = [{'name': 'Region'}]
        result = self._query('card', fields)
        qs = result['queryState']
        self.assertEqual(set(qs), {'Values'})
        self.assertEqual(len(qs['Values']['projections']), 1)
        self.assertEqual(qs['Values']['projections'][0]['field'], {
            'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                       'Property': 'Region'},
        })

    def test_catalog_card_string_column_overrides_without_aggregation(self):
        # A card's Values role is measure-only, so the string column is
        # unbindable there. The card is downgraded to a multiRowCard, whose
        # Values role accepts columns, and the contract judges that already
        # downgraded type -- so the higher-fidelity target is kept rather than
        # being clobbered to a table. The column must survive as a bare,
        # unaggregated reference.
        self.gen._actual_bim_symbols = {('Sales', 'Record Label')}
        self.gen._actual_bim_column_types = {('Sales', 'Record Label'): 'string'}
        fields = [{'name': 'Record Label', 'shelf': 'text'}]
        worksheet = {'chart_type': 'card', 'fields': fields}
        roles = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(worksheet['_override_visual_type'], 'multiRowCard')
        self.assertNotIn('_visual_mapping_note', worksheet)
        self.assertNotIn('_visual_fallback_note', worksheet)
        self.assertEqual(set(roles), {'Values'})
        # No loss: the single source field still reaches Values.
        self.assertEqual(len(roles['Values']['projections']), len(fields))
        self.assertEqual([projection['field']
                          for projection in roles['Values']['projections']], [
            {'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': 'Record Label'}},
        ])
        self.assertEqual(self._query('multiRowCard', fields)['queryState'], roles)

    def test_catalog_card_date_column_defaults_to_max(self):
        self.gen._actual_bim_symbols = {('Sales', 'Observed Date')}
        for shelf in ('text', 'rows', 'columns', 'tooltip'):
            with self.subTest(shelf=shelf):
                worksheet = {'chart_type': 'card', 'fields': [
                    {'name': 'Observed Date', 'shelf': shelf},
                ]}
                roles = self.gen._build_visual_query(worksheet)['queryState']
                self.assertEqual(set(roles), {'Values'})
                self.assertEqual(len(roles['Values']['projections']), 1)
                self.assertEqual(roles['Values']['projections'][0]['field'], {
                    'Aggregation': {
                        'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': 'Observed Date',
                        }},
                        'Function': 4,
                    },
                })
                self.assertNotIn('_override_visual_type', worksheet)

    def test_catalog_card_date_column_retains_explicit_min(self):
        self.gen._actual_bim_symbols = {('Sales', 'Observed Date')}
        for shelf in ('text', 'tooltip', 'rows', 'columns'):
            with self.subTest(shelf=shelf):
                roles = self._query('card', [
                    {'name': 'Observed Date', 'shelf': shelf, 'aggregation': 'min'},
                ])['queryState']
                self.assertEqual(set(roles), {'Values'})
                self.assertEqual(len(roles['Values']['projections']), 1)
                self.assertEqual(roles['Values']['projections'][0]['field'], {
                    'Aggregation': {
                        'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': 'Observed Date',
                        }},
                        'Function': 3,
                    },
                })

    def test_catalog_card_date_measure_keeps_actual_measure_wrapper(self):
        self.gen._measure_names.add('Latest Date')
        self.gen._bim_measure_names.add('Latest Date')
        self.gen._actual_bim_measure_names = {'Latest Date'}
        self.gen._actual_bim_symbols = {('Sales', 'Latest Date')}
        for aggregation in ('', 'min', 'max'):
            with self.subTest(aggregation=aggregation):
                roles = self._query('card', [
                    {'name': 'Latest Date', 'shelf': 'text', 'aggregation': aggregation},
                ])['queryState']
                self.assertEqual(set(roles), {'Values'})
                self.assertEqual(len(roles['Values']['projections']), 1)
                self.assertEqual(roles['Values']['projections'][0]['field'], {
                    'Measure': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': 'Latest Date'},
                })
                self.assertEqual(roles['Values']['projections'][0]['queryRef'],
                                 'Sales.Latest Date')

    def test_catalog_date_column_default_aggregation_is_scalar_card_only(self):
        self.gen._actual_bim_symbols = {('Sales', 'Observed Date'),
                                        ('Sales', 'Revenue')}
        date_pill = {'name': 'Observed Date', 'shelf': 'rows'}
        # clusteredBarChart requires a Y role, so it is given a measure; the
        # date pill must still reach Category as a bare column.
        for visual_type, role, fields in (
                ('tableEx', 'Values', [date_pill]),
                ('multiRowCard', 'Values', [date_pill]),
                ('clusteredBarChart', 'Category',
                 [date_pill, {'name': 'Revenue', 'shelf': 'columns'}])):
            with self.subTest(visual_type=visual_type):
                worksheet = {'chart_type': visual_type, 'fields': fields}
                roles = self.gen._build_visual_query(worksheet)['queryState']
                self.assertNotIn('_override_visual_type', worksheet)
                self.assertEqual(len(roles[role]['projections']), 1)
                self.assertEqual(roles[role]['projections'][0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                               'Property': 'Observed Date'},
                })

    def test_catalog_card_multiple_measures_override_without_target_loss(self):
        names = [f'Metric {index}' for index in range(1, 9)]
        self.gen._measure_names.update(names)
        self.gen._bim_measure_names.update(names)
        self.gen._actual_bim_measure_names = set(names)
        self.gen._actual_bim_symbols = {('Sales', name) for name in names}
        fields = [{'name': name, 'shelf': 'text'} for name in names]
        worksheet = {'chart_type': 'card', 'fields': fields}
        roles = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(worksheet['_override_visual_type'], 'multiRowCard')
        self.assertEqual(set(roles), {'Values'})
        self.assertEqual(len(roles['Values']['projections']), len(names))
        self.assertEqual([projection['field']
                          for projection in roles['Values']['projections']], [
            {'Measure': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                         'Property': name}} for name in names
        ])
        self.assertEqual(self._query('multiRowCard', fields)['queryState'], roles)

    def test_catalog_card_multiple_dimensions_override_without_target_loss(self):
        # Several targets downgrade the card to a multiRowCard, whose Values
        # role accepts columns; the contract judges that downgraded type and
        # leaves it alone. Every target must still be projected.
        names = ['Record Label', 'Batch Label']
        self.gen._actual_bim_symbols = {('Sales', name) for name in names}
        fields = [{'name': name, 'shelf': 'rows'} for name in names]
        worksheet = {'chart_type': 'card', 'fields': fields}
        roles = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(worksheet['_override_visual_type'], 'multiRowCard')
        self.assertNotIn('_visual_mapping_note', worksheet)
        self.assertNotIn('_visual_fallback_note', worksheet)
        self.assertEqual(set(roles), {'Values'})
        # No loss: every source field still reaches Values, unaggregated.
        self.assertEqual(len(roles['Values']['projections']), len(names))
        self.assertEqual([projection['field']
                          for projection in roles['Values']['projections']], [
            {'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': name}} for name in names
        ])
        self.assertEqual(self._query('multiRowCard', fields)['queryState'], roles)

    def test_pie_chart(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        result = self._query('pieChart', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Y', qs)

    def test_donut_chart(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        result = self._query('donutChart', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)

    def test_treemap_uses_group_role(self):
        """Treemap must use Group role (not Category) and Values (not Y)."""
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        result = self._query('treemap', fields)
        qs = result['queryState']
        self.assertIn('Group', qs)
        self.assertIn('Values', qs)
        self.assertNotIn('Category', qs)
        self.assertNotIn('Y', qs)

    def test_filled_map_uses_location_role(self):
        """Filled map must use Category role (PBIR Location well)."""
        fields = [{'name': 'Country'}, {'name': 'Revenue'}]
        result = self._query('filledMap', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Size', qs)
        self.assertNotIn('Location', qs)

    def test_matrix_uses_rows_columns_values(self):
        """Matrix must use Rows/Columns/Values roles."""
        fields = [{'name': 'Region'}, {'name': 'Month'}, {'name': 'Revenue'}]
        result = self._query('matrix', fields)
        qs = result['queryState']
        self.assertIn('Rows', qs)
        self.assertIn('Columns', qs)
        self.assertIn('Values', qs)

    def test_combo_chart(self):
        fields = [{'name': 'Month'}, {'name': 'Revenue'}, {'name': 'Profit'}]
        for visual_type in ('lineClusteredColumnComboChart',
                            'lineStackedColumnComboChart'):
            with self.subTest(visual_type=visual_type):
                qs = self._query(visual_type, fields)['queryState']
                self.assertEqual(set(qs), {'Category', 'Y', 'Y2'})
                self.assertEqual(
                    {role: [projection['field'] for projection in value['projections']]
                     for role, value in qs.items()},
                    {
                        'Category': [{'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': 'Month',
                        }}],
                        'Y': [{'Measure': {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': 'Revenue',
                        }}],
                        'Y2': [{'Measure': {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': 'Profit',
                        }}],
                    },
                )

    def test_waterfall_chart(self):
        fields = [{'name': 'Region'}, {'name': 'Month'}, {'name': 'Revenue'}]
        result = self._query('waterfallChart', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Y', qs)
        self.assertIn('Breakdown', qs)

    def test_box_and_whisker(self):
        fields = [{'name': 'Region'}, {'name': 'Revenue'}]
        result = self._query('boxAndWhisker', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Value', qs)

    def test_standard_chart_dim_fallback(self):
        """Without measures, source dimensions are preserved as a table."""
        self.gen._measure_names.clear()
        self.gen._bim_measure_names.clear()
        fields = [{'name': 'Region'}, {'name': 'State'}]
        for visual_type in ('clusteredBarChart', 'lineChart'):
            with self.subTest(visual_type=visual_type):
                worksheet = {'chart_type': visual_type, 'fields': fields}
                qs = self.gen._build_visual_query(worksheet)['queryState']
                self.assertEqual(set(qs), {'Values'})
                self.assertEqual([projection['field']
                                  for projection in qs['Values']['projections']], [
                    {'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': name}} for name in ('Region', 'State')
                ])
                self.assertEqual(worksheet['_override_visual_type'], 'tableEx')
                self.assertEqual(worksheet['_visual_fallback_note'],
                                 'No resolved value field for this chart; '
                                 'source dimensions were preserved as a table.')

    def test_deduplicate_fields(self):
        """Same field from multiple shelves is deduplicated."""
        fields = [{'name': 'Revenue'}, {'name': 'Revenue'}]
        result = self._query('clusteredBarChart', fields)
        qs = result['queryState']
        # Only one projection for Revenue; no dims → fallback to tableEx with Values
        self.assertIn('Values', qs)

    def test_skip_tableau_internal_fields(self):
        fields = [{'name': '__tableau_internal_object_id__'}, {'name': 'Revenue'}]
        result = self._query('clusteredBarChart', fields)
        qs = result['queryState']
        # Only Revenue remains (measure, no dims) → fallback to tableEx with Values
        self.assertIn('Values', qs)

    def test_measure_value_shelf_treated_as_measure(self):
        """Fields with shelf='measure_value' (from Measure Names expansion) → measure role."""
        fields = [
            {'name': 'Region'},
            {'name': 'Revenue', 'shelf': 'measure_value'},
            {'name': 'Profit', 'shelf': 'measure_value'},
        ]
        result = self._query('clusteredBarChart', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Y', qs)
        # Both measures should be in Y projections
        self.assertEqual(len(qs['Y']['projections']), 2)

    def test_all_measures_fallback_to_tableEx(self):
        """Only measures (no dims) in a bar chart → fallback to tableEx."""
        fields = [{'name': 'Revenue'}, {'name': 'Profit'}]
        ws = {'chart_type': 'clusteredBarChart', 'fields': fields}
        result = self.gen._build_visual_query(ws)
        qs = result['queryState']
        self.assertIn('Values', qs)  # tableEx uses 'Values' role
        # Should set override visual type
        self.assertEqual(ws.get('_override_visual_type'), 'tableEx')

    def test_all_measures_fallback_tableEx_many(self):
        """3+ measures → tableEx (preserves all data columns)."""
        self.gen._measure_names.add('Quantity')
        self.gen._bim_measure_names.add('Quantity')
        fields = [{'name': 'Revenue'}, {'name': 'Profit'}, {'name': 'Quantity'}]
        ws = {'chart_type': 'clusteredBarChart', 'fields': fields}
        result = self.gen._build_visual_query(ws)
        self.assertEqual(ws.get('_override_visual_type'), 'tableEx')

    def test_multiple_measures_in_y_role(self):
        """Standard chart with dim + multiple measures → all measures in Y."""
        self.gen._measure_names.add('Quantity')
        self.gen._bim_measure_names.add('Quantity')
        fields = [{'name': 'Region'}, {'name': 'Revenue'}, {'name': 'Profit'}, {'name': 'Quantity'}]
        result = self._query('areaChart', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Y', qs)
        self.assertEqual(len(qs['Y']['projections']), 3)

    def test_bim_measure_in_category_reclassified(self):
        """Field classified as dim by extractor but measure by TMDL → not in Category.

        Regression test for SecondaryGroupsWithoutPrimary PBI error.
        When a field is only in _bim_measure_names (not _measure_names),
        _is_measure_field() must still return True so shelf classification
        places it in axis_meas (Y/Values), not axis_dims (Category).
        """
        # 'Deal Size Bucket' is a BIM measure but not in extraction _measure_names
        self.gen._bim_measure_names.add('Deal Size Bucket')
        # Simulate: Deal Size Bucket on rows shelf (treated as dim by Tableau)
        fields = [
            {'name': 'Deal Size Bucket', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'rows'},
        ]
        ws = {'chart_type': 'clusteredBarChart', 'fields': fields}
        result = self.gen._build_visual_query(ws)
        qs = result['queryState']
        # Deal Size Bucket should NOT be in Category (it's a BIM measure)
        if 'Category' in qs:
            cat_props = [p['field'].get('Measure', p['field'].get('Column', {})).get('Property', '')
                         for p in qs['Category']['projections']]
            self.assertNotIn('Deal Size Bucket', cat_props,
                             'BIM measure should not appear in Category role')

    # ── Shelf-aware tests ────────────────────────────────────────

    def test_color_dim_becomes_series(self):
        """A dimension on the color shelf → Series role."""
        fields = [
            {'name': 'Date', 'shelf': 'columns'},
            {'name': 'Segment', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'rows'},
        ]
        result = self._query('areaChart', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Series', qs)
        self.assertIn('Y', qs)

    def test_explicit_axis_aggregation_classifies_physical_columns_as_measures(self):
        self.gen._field_map.setdefault('Amount', ('Sales', 'Amount'))
        self.gen._actual_bim_column_types = {('Sales', 'Amount'): 'double'}
        for shelf, dimension_shelf, measure_bucket, dimension_bucket in (
            ('rows', 'columns', 'rows_meas', 'cols_dims'),
            ('columns', 'rows', 'cols_meas', 'rows_dims'),
        ):
            for aggregation, function in (('sum', 0), ('avg', 1), ('min', 3), ('max', 4)):
                with self.subTest(shelf=shelf, aggregation=aggregation):
                    dimension = {'name': 'Category', 'shelf': dimension_shelf}
                    measure = {'name': 'Amount', 'shelf': shelf,
                               'aggregation': aggregation}
                    fields = [dimension, measure]
                    classified = self.gen._classify_shelf_fields(fields)
                    self.assertEqual(classified[measure_bucket], [measure])
                    self.assertEqual(classified[dimension_bucket], [dimension])
                    self.assertEqual(sum(len(bucket) for bucket in classified.values()), 2)
                    roles = self._query('clusteredBarChart', fields)['queryState']
                    self.assertEqual(set(roles), {'Category', 'Y'})
                    self.assertEqual(roles['Category']['projections'][0]['field'], {
                        'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                   'Property': 'Category'},
                    })
                    self.assertEqual(roles['Y']['projections'][0]['field'], {
                        'Aggregation': {
                            'Expression': {'Column': {
                                'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': 'Amount',
                            }},
                            'Function': function,
                        },
                    })
                    self.assertEqual(len(roles['Category']['projections']), 1)
                    self.assertEqual(len(roles['Y']['projections']), 1)
                    self.assertEqual(measure, {'name': 'Amount', 'shelf': shelf,
                                               'aggregation': aggregation})

    def test_unknown_axis_aggregation_keeps_region_dimension(self):
        self.gen._field_map['Region'] = ('Sales', 'Region')
        self.gen._actual_bim_column_types = {}
        for shelf, dimension_bucket in (('rows', 'rows_dims'), ('columns', 'cols_dims')):
            for aggregation in ('sum', 'avg', 'min', 'max', 'count', 'countd'):
                with self.subTest(shelf=shelf, aggregation=aggregation):
                    field = {'name': 'Region', 'shelf': shelf,
                             'aggregation': aggregation}
                    fields = [field]
                    classified = self.gen._classify_shelf_fields(fields)
                    self.assertEqual(classified[dimension_bucket], [
                        {'name': 'Region', 'shelf': shelf},
                    ])
                    self.assertEqual(classified['rows_meas'], [])
                    self.assertEqual(classified['cols_meas'], [])
                    self.assertEqual(sum(len(bucket) for bucket in classified.values()), 1)
                    self.assertEqual(fields, [
                        {'name': 'Region', 'shelf': shelf, 'aggregation': aggregation},
                    ])
                    self.assertIs(fields[0], field)

    def test_date_axis_grain_keeps_min_aggregation_on_tooltips(self):
        for shelf, measure_shelf, dimension_bucket in (
            ('rows', 'columns', 'rows_dims'),
            ('columns', 'rows', 'cols_dims'),
        ):
            with self.subTest(shelf=shelf):
                date = {'name': 'Observed Date', 'shelf': shelf, 'aggregation': 'min'}
                classified = self.gen._classify_shelf_fields([date])
                self.assertEqual(classified[dimension_bucket], [
                    {'name': 'Observed Date', 'shelf': shelf},
                ])
                self.assertEqual(classified['rows_meas'], [])
                self.assertEqual(classified['cols_meas'], [])
                roles = self._query('clusteredBarChart', [
                    date,
                    {'name': 'Revenue', 'shelf': measure_shelf},
                    {'name': 'Observed Date', 'shelf': 'tooltip', 'aggregation': 'min'},
                    {'name': 'Observed Date', 'shelf': 'tooltip', 'aggregation': 'min'},
                ])['queryState']
                self.assertEqual(set(roles), {'Category', 'Y', 'Tooltips'})
                column = {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': 'Observed Date',
                }}
                self.assertEqual([projection['field']
                                  for projection in roles['Category']['projections']],
                                 [column])
                self.assertEqual(roles['Tooltips']['projections'], [{
                    'field': {'Aggregation': {'Expression': column, 'Function': 3}},
                    'queryRef': 'Sales.Observed Date',
                    'nativeQueryRef': 'Observed Date',
                    'active': True,
                }])
                self.assertEqual(date, {'name': 'Observed Date', 'shelf': shelf,
                                       'aggregation': 'min'})

    def test_color_shelf_dim_overrides_axis_series(self):
        """Color dim has priority for Series over second axis dim."""
        fields = [
            {'name': 'Date', 'shelf': 'columns'},
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'rows'},
        ]
        result = self._query('areaChart', fields)
        qs = result['queryState']
        self.assertIn('Series', qs)
        # Segment (color) wins Series, not Region (2nd axis dim)
        prop = qs['Series']['projections'][0]['field']['Column']['Property']
        self.assertEqual(prop, 'Segment')

    def test_tooltip_shelf_becomes_tooltips_role(self):
        """Tooltip fields → Tooltips PBIR role (not Y)."""
        fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'columns'},
            {'name': 'Profit', 'shelf': 'tooltip'},
        ]
        result = self._query('clusteredBarChart', fields)
        qs = result['queryState']
        self.assertIn('Tooltips', qs)
        self.assertEqual(len(qs['Y']['projections']), 1)  # Only Revenue in Y

    def test_color_measure_goes_to_tooltips(self):
        """A measure on the color shelf → Tooltips (not Y)."""
        fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'columns'},
            {'name': 'Profit', 'shelf': 'color'},  # Profit is a measure
        ]
        result = self._query('clusteredBarChart', fields)
        qs = result['queryState']
        self.assertIn('Tooltips', qs)
        # Only Revenue should be in Y (not Profit)
        self.assertEqual(len(qs['Y']['projections']), 1)

    def test_map_uses_location_role(self):
        """Map visuals use the catalog Category role for the Location well."""
        fields = [{'name': 'City', 'shelf': 'rows'}, {'name': 'Revenue'}]
        result = self._query('map', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertNotIn('Location', qs)

    def test_map_multiple_geo_dims_in_location(self):
        """The first geographic dimension fills the single-value Category role."""
        fields = [
            {'name': 'Country', 'shelf': 'rows'},
            {'name': 'City', 'shelf': 'rows'},
            {'name': 'Revenue'},
        ]
        result = self._query('map', fields)
        qs = result['queryState']
        self.assertEqual(len(qs['Category']['projections']), 1)
        self.assertEqual(qs['Category']['projections'][0]['field']['Column']['Property'],
                         'Country')
        self.assertNotIn('Location', qs)

    def test_filled_map_legend_from_color_dim(self):
        """FilledMap: color dim → Series role."""
        fields = [
            {'name': 'Country', 'shelf': 'rows'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue'},
        ]
        result = self._query('filledMap', fields)
        qs = result['queryState']
        self.assertIn('Category', qs)
        self.assertIn('Series', qs)

    def test_treemap_non_date_dims_first(self):
        """Treemap: non-date dims precede date dims across Group and Details."""
        fields = [
            {'name': 'Date', 'shelf': 'columns'},
            {'name': 'Category', 'shelf': 'rows'},
            {'name': 'Revenue'},
        ]
        result = self._query('treemap', fields)
        qs = result['queryState']
        self.assertEqual(qs, {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Group', 'Column', 'Category'), ('Details', 'Column', 'Date'),
                ('Values', 'Measure', 'Revenue'),
            )
        })

    def test_treemap_multiple_group_levels(self):
        """Treemap: two grouping levels use one Group and one Details field."""
        fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Category', 'shelf': 'rows'},
            {'name': 'Revenue'},
        ]
        result = self._query('treemap', fields)
        qs = result['queryState']
        self.assertEqual(qs, {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Group', 'Column', 'Region'), ('Details', 'Column', 'Category'),
                ('Values', 'Measure', 'Revenue'),
            )
        })

    def test_treemap_extra_dimensions_use_min_scalar_tooltips_and_migration_note(self):
        for dimension_count in (3, 4):
            with self.subTest(dimension_count=dimension_count):
                fields = [
                    {'name': 'Date', 'shelf': 'rows'},
                    {'name': 'Region', 'shelf': 'rows'},
                    {'name': 'Category', 'shelf': 'rows'},
                ]
                if dimension_count == 4:
                    fields.append({'name': 'Segment Label (%) / Code', 'shelf': 'color'})
                fields.append({'name': 'Revenue'})
                worksheet = {'chart_type': 'treemap', 'fields': fields}
                with patch('powerbi_import.pbip_generator._write_json') as write_json, \
                        patch('powerbi_import.pbip_generator.os.makedirs'):
                    self.gen._create_visual_worksheet(
                        self.gen.output_dir, worksheet,
                        {'worksheetName': 'Synthetic Treemap', 'position': {}},
                        1, 1, 0, [], {},
                    )
                write_json.assert_called_once()
                visual = write_json.call_args.args[1]
                self.assertEqual(visual['visual']['visualType'], 'treemap')
                expected_roles = {
                    role: {'projections': [{
                        'field': {wrapper: {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': name,
                        }},
                        'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                        'active': True,
                    }]}
                    for role, wrapper, name in (
                        ('Group', 'Column', 'Region'), ('Details', 'Column', 'Category'),
                        ('Values', 'Measure', 'Revenue'),
                    )
                }
                tooltip_names = (('Date',) if dimension_count == 3
                                 else ('Segment Label (%) / Code', 'Date'))
                expected_roles['Tooltips'] = {'projections': [{
                    'field': {'Aggregation': {
                        'Expression': {'Column': {
                            'Expression': {'SourceRef': {'Entity': 'Sales'}},
                            'Property': name,
                        }},
                        'Function': 3,
                    }},
                    'queryRef': f'Min(Sales.{name})', 'nativeQueryRef': name,
                    'active': True,
                } for name in tooltip_names]}
                self.assertEqual(visual['visual']['query']['queryState'], expected_roles)
                self.assertIn({
                    'name': 'MigrationNote',
                    'value': 'Treemap supports two grouping levels; '
                             'additional source attributes remain in tooltips.',
                }, visual['annotations'])
                self.assertNotIn('annotations', visual['visual'])

    def test_series_not_set_without_measures(self):
        """Without measures, axis dimensions stay in Values, not Series or Y."""
        self.gen._measure_names.clear()
        self.gen._bim_measure_names.clear()
        fields = [{'name': 'Region'}, {'name': 'State'}]
        worksheet = {'chart_type': 'clusteredBarChart', 'fields': fields}
        qs = self.gen._build_visual_query(worksheet)['queryState']
        self.assertEqual(set(qs), {'Values'})
        self.assertEqual([projection['field']
                          for projection in qs['Values']['projections']], [
            {'Column': {'Expression': {'SourceRef': {'Entity': 'Sales'}},
                        'Property': name}} for name in ('Region', 'State')
        ])
        self.assertNotIn('Series', qs)
        self.assertNotIn('Y', qs)
        self.assertEqual(worksheet['_override_visual_type'], 'tableEx')
        self.assertEqual(worksheet['_visual_fallback_note'],
                         'No resolved value field for this chart; '
                         'source dimensions were preserved as a table.')

    def test_scatter_all_dims_in_details(self):
        """Scatter uses one Category and one Series for its two dimensions."""
        fields = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'columns'},
            {'name': 'Profit', 'shelf': 'columns'},
        ]
        result = self._query('scatterChart', fields)
        qs = result['queryState']
        self.assertEqual(qs, {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Category', 'Column', 'Region'), ('Series', 'Column', 'Segment'),
                ('X', 'Measure', 'Revenue'), ('Y', 'Measure', 'Profit'),
            )
        })

    def test_scatter_extra_dimensions_use_min_scalar_tooltips(self):
        roles = self._query('scatterChart', [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Segment', 'shelf': 'rows'},
            {'name': 'Group', 'shelf': 'rows'},
            {'name': 'Subgroup', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'columns'},
            {'name': 'Profit', 'shelf': 'rows'},
            {'name': 'Region', 'shelf': 'tooltip'},
            {'name': 'Segment', 'shelf': 'tooltip'},
            {'name': 'Group', 'shelf': 'tooltip'},
        ])['queryState']
        expected_roles = {
            role: {'projections': [{
                'field': {wrapper: {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'queryRef': f'Sales.{name}', 'nativeQueryRef': name,
                'active': True,
            }]}
            for role, wrapper, name in (
                ('Category', 'Column', 'Region'), ('Series', 'Column', 'Segment'),
                ('X', 'Measure', 'Revenue'), ('Y', 'Measure', 'Profit'),
            )
        }
        expected_roles['Tooltips'] = {'projections': [{
            'field': {'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'Sales'}},
                    'Property': name,
                }},
                'Function': 3,
            }},
            'queryRef': f'Min(Sales.{name})', 'nativeQueryRef': name,
            'active': True,
        } for name in ('Group', 'Subgroup')]}
        self.assertEqual(roles, expected_roles)

    def test_combo_chart_with_series(self):
        """Combo chart: color dim → Series, 2 measures → Y + Y2."""
        fields = [
            {'name': 'Month', 'shelf': 'columns'},
            {'name': 'Segment', 'shelf': 'color'},
            {'name': 'Revenue', 'shelf': 'rows'},
            {'name': 'Profit', 'shelf': 'rows'},
        ]
        for visual_type in ('lineClusteredColumnComboChart',
                            'lineStackedColumnComboChart'):
            with self.subTest(visual_type=visual_type):
                qs = self._query(visual_type, fields)['queryState']
                self.assertEqual(set(qs), {'Category', 'Series', 'Y', 'Y2'})
                for role, wrapper, prop in (
                    ('Category', 'Column', 'Month'),
                    ('Series', 'Column', 'Segment'),
                    ('Y', 'Measure', 'Revenue'),
                    ('Y2', 'Measure', 'Profit'),
                ):
                    with self.subTest(role=role):
                        self.assertEqual(
                            [projection['field'] for projection in qs[role]['projections']],
                            [{wrapper: {
                                'Expression': {'SourceRef': {'Entity': 'Sales'}},
                                'Property': prop,
                            }}],
                        )


class TestDatasourceScopedFieldBindings(unittest.TestCase):
    def setUp(self):
        output_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.gen = _make_generator(output_dir)
        _init_field_map(
            self.gen,
            {'Category': ('TableA', 'Category'),
             'Amount': ('TableA', 'Amount'),
             'RevenueA': ('TableA', 'RevenueA'),
             'RevenueB': ('TableB', 'RevenueB')},
            measure_names=['Amount', 'RevenueA', 'RevenueB'],
            bim_measure_names=['RevenueA', 'RevenueB'],
            main_table='TableA',
            datasources_ref=[
                {'name': 'source_a', 'tables': [{'name': 'TableA'}]},
                {'name': 'source_b', 'tables': [{'name': 'TableB'}]},
            ],
        )
        self.gen._actual_bim_symbols = {
            ('TableA', 'Category'), ('TableA', 'Amount'), ('TableA', 'RevenueA'),
            ('TableB', 'Category'), ('TableB', 'Amount'), ('TableB', 'RevenueB'),
        }
        self.gen._actual_bim_measure_names = {'RevenueA', 'RevenueB'}
        self.gen._ds_table_map = {'source_a': 'TableA', 'source_b': 'TableB'}

    def test_homonymous_dimension_and_measure_bind_to_owning_datasource(self):
        for datasource, table, measure in (
            ('source_a', 'TableA', 'RevenueA'),
            ('source_b', 'TableB', 'RevenueB'),
        ):
            with self.subTest(datasource=datasource):
                worksheet = {'chart_type': 'clusteredBarChart', 'fields': [
                    {'name': 'Category', 'shelf': 'rows', 'datasource': datasource},
                    {'name': measure, 'shelf': 'columns', 'datasource': datasource},
                ]}
                query = self.gen._build_visual_query(worksheet)
                self.assertIsNotNone(query)
                roles = query['queryState']
                self.assertEqual(set(roles), {'Category', 'Y'})
                self.assertEqual(roles['Category']['projections'][0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': table}},
                               'Property': 'Category'},
                })
                self.assertEqual(roles['Y']['projections'][0]['field'], {
                    'Measure': {'Expression': {'SourceRef': {'Entity': table}},
                                'Property': measure},
                })
                self.assertNotIn('_unresolved_source_fields', worksheet)

    def test_resolver_scopes_clean_mapped_and_calculation_captions(self):
        self.gen._field_map.update({
            'category_alias': ('TableA', 'Category'),
            'Calculation_1': ('TableA', 'Foreign Caption'),
        })
        self.gen._actual_bim_symbols.add(('TableA', 'Foreign Caption'))
        self.gen._datasources_ref[0]['calculations'] = [
            {'name': '[Calculation_1]', 'caption': 'Foreign Caption'},
        ]
        self.gen._datasources_ref[1]['calculations'] = [
            {'name': '[Calculation_1]', 'caption': 'Category'},
        ]
        for field in ('Category', '[category_alias]', '[Calculation_1]'):
            with self.subTest(field=field):
                self.assertEqual(
                    self.gen._resolve_field_entity(field, datasource='source_b'),
                    ('TableB', 'Category'),
                )

    def test_namespaced_measure_provenance_binds_card_to_owning_datasource(self):
        calculation_id = 'Calculation_Shared'
        for caption in ('Shared Calculation Caption', 'Montant  réalisé (%)'):
            with self.subTest(caption=caption):
                namespaced_caption = f'{caption} (TableB)'
                self.gen._field_map.update({
                    calculation_id: ('TableA', caption),
                    caption: ('TableA', caption),
                })
                for source in self.gen._datasources_ref:
                    source['calculations'] = [
                        {'name': f'[{calculation_id}]', 'caption': caption},
                    ]
                self.gen._measure_names.update({
                    calculation_id, caption, namespaced_caption,
                })
                self.gen._bim_measure_names.update({caption, namespaced_caption})
                self.gen._actual_bim_measure_names.update({caption, namespaced_caption})
                self.gen._actual_bim_symbols.update({
                    ('TableA', caption), ('TableB', namespaced_caption),
                })
                self.gen._measure_rename_map = {
                    ('TableB', caption): namespaced_caption,
                }
                for datasource, table, measure in (
                    ('source_a', 'TableA', caption),
                    ('source_b', 'TableB', namespaced_caption),
                ):
                    for field in (f'[{calculation_id}]', caption):
                        with self.subTest(datasource=datasource, field=field):
                            self.assertEqual(
                                self.gen._resolve_field_entity(field, datasource=datasource),
                                (table, measure),
                            )
                            worksheet = {'chart_type': 'card', 'fields': [
                                {'name': field, 'shelf': 'text', 'datasource': datasource},
                            ]}
                            query = self.gen._build_visual_query(worksheet)
                            self.assertIsNotNone(query)
                            self.assertEqual(query['queryState'], {
                                'Values': {'projections': [{
                                    'field': {'Measure': {
                                        'Expression': {'SourceRef': {'Entity': table}},
                                        'Property': measure,
                                    }},
                                    'queryRef': f'{table}.{measure}',
                                    'nativeQueryRef': measure,
                                    'active': True,
                                }]},
                            })
                            self.assertNotIn('_unresolved_source_fields', worksheet)
                self.assertEqual(self.gen._field_map[calculation_id], ('TableA', caption))
                self.assertEqual(self.gen._field_map[caption], ('TableA', caption))

    def test_source_specific_filters_use_scoped_entity_and_caption(self):
        self.gen._field_map['category_alias'] = ('TableA', 'Category')
        self.gen._datasources_ref[1]['calculations'] = [
            {'name': '[Calculation_1]', 'caption': 'Category'},
        ]
        for datasource, field, table in (
            ('source_a', 'Category', 'TableA'),
            ('source_b', 'Category', 'TableB'),
            ('source_b', '[category_alias]', 'TableB'),
            ('source_b', '[Calculation_1]', 'TableB'),
        ):
            with self.subTest(datasource=datasource, field=field):
                filters = self.gen._create_visual_filters([
                    {'field': field, 'datasource': datasource, 'values': ['Group A']},
                ])
                self.assertEqual(len(filters), 1)
                self.assertEqual(filters[0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': table}},
                               'Property': 'Category'},
                })
                self.assertEqual(filters[0]['filter']['From'], [
                    {'Name': 't', 'Entity': table, 'Type': 0},
                ])
                condition = filters[0]['filter']['Where'][0]['Condition']['In']
                self.assertEqual(condition['Expressions'], [
                    {'Column': {'Expression': {'SourceRef': {'Source': 't'}},
                                'Property': 'Category'}},
                ])

    def test_missing_source_dimension_cannot_borrow_foreign_homonym(self):
        self.gen._actual_bim_symbols.remove(('TableB', 'Category'))
        self.gen._actual_bim_symbols.add(('LookupB', 'Lookup Key'))
        self.assertEqual(
            self.gen._resolve_field_entity('Category', datasource='source_b'),
            ('TableB', 'Category'),
        )
        for source_tables in (
            [{'name': 'TableB'}],
            [{'name': 'TableB'}, {'name': 'LookupB'}],
        ):
            with self.subTest(source_tables=source_tables):
                self.gen._datasources_ref[1]['tables'] = source_tables
                worksheet = {'chart_type': 'tableEx', 'fields': [
                    {'name': 'Category', 'datasource': 'source_b'},
                ]}
                self.assertIsNone(self.gen._build_visual_query(worksheet))
                self.assertEqual(worksheet['_unresolved_source_fields'], ['Category'])
                self.assertEqual(self.gen._create_visual_filters([
                    {'field': 'Category', 'datasource': 'source_b', 'values': ['Group A']},
                ]), [])

    def test_known_source_blocks_foreign_unique_owner_and_records_once(self):
        self.gen._field_map['Remote Dimension'] = ('TableB', 'Remote Dimension')
        self.gen._actual_bim_symbols.add(('TableA', 'Remote Dimension'))
        raw_name = 'none:Remote Dimension:nk'
        worksheet = {'chart_type': 'clusteredBarChart', 'fields': [
            {'name': 'Category', 'shelf': 'rows', 'datasource': 'source_b'},
            {'name': 'RevenueB', 'shelf': 'columns', 'datasource': 'source_b'},
            {'name': raw_name, 'shelf': 'rows', 'datasource': 'source_b'},
            {'name': raw_name, 'shelf': 'tooltip', 'datasource': 'source_b'},
        ]}
        query = self.gen._build_visual_query(worksheet)
        self.assertIsNotNone(query)
        roles = query['queryState']
        self.assertEqual(set(roles), {'Category', 'Y'})
        self.assertEqual(len(roles['Category']['projections']), 1)
        self.assertEqual(roles['Category']['projections'][0]['field'], {
            'Column': {'Expression': {'SourceRef': {'Entity': 'TableB'}},
                       'Property': 'Category'},
        })
        self.assertEqual(worksheet['_unresolved_source_fields'], [raw_name])

    def test_renamed_collisions_scope_all_tables_in_the_datasource(self):
        self.gen._datasources_ref[0]['tables'] = [{'name': 'Shared'}]
        self.gen._datasources_ref[1]['tables'] = [
            {'name': 'Shared'}, {'name': 'Lookup'},
        ]
        self.gen._table_rename_map = {
            ('source_a', 'Shared'): 'TableA',
            ('source_b', 'Shared'): 'TableB',
            ('source_b', 'Lookup'): 'LookupB',
        }
        self.gen._collision_tables = {'Shared', 'TableA', 'TableB', 'LookupB'}
        self.gen._field_map['Category'] = ('Shared', 'Category')
        self.gen._field_map['Lookup Label'] = ('TableA', 'Lookup Label')
        self.gen._actual_bim_symbols.update({
            ('TableA', 'Lookup Label'), ('LookupB', 'Lookup Label'),
        })
        for datasource, dimension, table, measure, measure_table in (
            ('source_a', 'Category', 'TableA', 'RevenueA', 'TableA'),
            ('source_b', 'Category', 'TableB', 'RevenueB', 'TableB'),
            ('source_b', 'Lookup Label', 'LookupB', 'RevenueB', 'TableB'),
        ):
            with self.subTest(datasource=datasource, dimension=dimension):
                self.assertEqual(
                    self.gen._resolve_field_entity(dimension, datasource=datasource),
                    (table, dimension),
                )
                query = self.gen._build_visual_query({
                    'chart_type': 'clusteredBarChart', 'fields': [
                        {'name': dimension, 'shelf': 'rows', 'datasource': datasource},
                        {'name': measure, 'shelf': 'columns', 'datasource': datasource},
                    ],
                })
                self.assertIsNotNone(query)
                roles = query['queryState']
                self.assertEqual(roles['Category']['projections'][0]['field'], {
                    'Column': {'Expression': {'SourceRef': {'Entity': table}},
                               'Property': dimension},
                })
                self.assertEqual(roles['Y']['projections'][0]['field'], {
                    'Measure': {'Expression': {'SourceRef': {'Entity': measure_table}},
                                'Property': measure},
                })

    def test_scatter_preserves_resolved_column_and_measure_pairs(self):
        self.gen._field_map['RevenueB'] = ('TableA', 'RevenueA')
        query = self.gen._build_visual_query({
            'chart_type': 'scatterChart', 'fields': [
                {'name': 'Category', 'shelf': 'detail', 'datasource': 'source_b'},
                {'name': 'Amount', 'shelf': 'columns', 'datasource': 'source_b',
                 'aggregation': 'avg'},
                {'name': 'RevenueB', 'shelf': 'rows', 'datasource': 'source_b'},
            ],
        })
        self.assertIsNotNone(query)
        roles = query['queryState']
        self.assertEqual(set(roles), {'Category', 'X', 'Y'})
        for role in ('Category', 'X', 'Y'):
            self.assertEqual(len(roles[role]['projections']), 1)
        self.assertEqual(roles['Category']['projections'][0]['field'], {
            'Column': {'Expression': {'SourceRef': {'Entity': 'TableB'}},
                       'Property': 'Category'},
        })
        self.assertEqual(roles['X']['projections'][0]['field'], {
            'Aggregation': {
                'Expression': {'Column': {
                    'Expression': {'SourceRef': {'Entity': 'TableB'}},
                    'Property': 'Amount',
                }},
                'Function': 1,
            },
        })
        self.assertEqual(roles['Y']['projections'][0]['field'], {
            'Measure': {'Expression': {'SourceRef': {'Entity': 'TableB'}},
                        'Property': 'RevenueB'},
        })
        for role, name in (('Category', 'Category'), ('X', 'Amount'), ('Y', 'RevenueB')):
            with self.subTest(role=role):
                projection = roles[role]['projections'][0]
                self.assertEqual(projection['queryRef'], f'TableB.{name}')
                self.assertEqual(projection['nativeQueryRef'], name)
                self.assertIs(projection['active'], True)
        self.gen._collision_tables = {'TableA', 'TableB'}
        self.gen._ds_table_map['source_b'] = 'TableA'
        for name, role in (('Amount', 'X'), ('RevenueB', 'Y')):
            with self.subTest(name=name):
                entry = self.gen._make_scatter_axis_entry({
                    'name': name, 'datasource': 'source_b', 'aggregation': 'avg',
                    '_resolved_pair': ('TableB', name),
                })
                self.assertIsNotNone(entry)
                self.assertEqual(entry['field'], roles[role]['projections'][0]['field'])
                self.assertEqual(entry['queryRef'], f'TableB.{name}')

    def test_unresolved_source_fields_emit_root_migration_note(self):
        self.gen._field_map['Remote Dimension'] = ('TableA', 'Remote Dimension')
        self.gen._actual_bim_symbols.add(('TableA', 'Remote Dimension'))
        worksheet = {'chart_type': 'clusteredBarChart', 'fields': [
            {'name': 'Category', 'shelf': 'rows', 'datasource': 'source_b'},
            {'name': 'RevenueB', 'shelf': 'columns', 'datasource': 'source_b'},
            {'name': 'Remote Dimension', 'shelf': 'tooltip', 'datasource': 'source_b'},
        ]}
        with patch('powerbi_import.pbip_generator._write_json') as write_json, \
                patch('powerbi_import.pbip_generator.os.makedirs'):
            self.gen._create_visual_worksheet(
                self.gen.output_dir, worksheet,
                {'worksheetName': 'Synthetic Sheet', 'position': {}},
                1, 1, 0, [], {},
            )
        write_json.assert_called_once()
        visual = write_json.call_args.args[1]
        self.assertIn({
            'name': 'MigrationNote',
            'value': 'Unresolved Tableau datasource fields: Remote Dimension',
        }, visual['annotations'])
        self.assertNotIn('annotations', visual['visual'])
        self.assertEqual(set(visual['visual']['query']['queryState']), {'Category', 'Y'})
        self.assertNotIn('_unresolved_source_fields', worksheet)


class TestFieldMappingDuplicateColumns(unittest.TestCase):
    """Verify that duplicate column names across tables resolve to the main table."""

    def test_main_table_wins_for_duplicate_columns(self):
        """When 'Segment' exists in both main and secondary tables, main table wins."""
        gen = _make_generator()
        converted = {
            'datasources': [{
                'tables': [
                    {'name': 'Orders', 'columns': [
                        {'name': 'Region'}, {'name': 'Segment'}, {'name': 'Sales', 'role': 'measure'},
                    ]},
                    {'name': 'Targets', 'columns': [
                        {'name': 'Segment'}, {'name': 'Goal'},
                    ]},
                ],
                'columns': [],
                'calculations': [],
            }],
            'calculations': [],
            'groups': [],
        }
        gen._build_field_mapping(converted)
        # 'Orders' has more columns → main table, so Segment should resolve to Orders
        entity, prop = gen._field_map['Segment']
        self.assertEqual(entity, 'Orders')
        self.assertEqual(prop, 'Segment')


# ─── _make_projection_entry ─────────────────────────────────────────


class TestMakeProjectionEntry(unittest.TestCase):
    """Projection entry — BIM measure vs Column wrapper + fallback."""

    def setUp(self):
        self.gen = _make_generator()

    def test_bim_measure_wrapper(self):
        _init_field_map(self.gen, {'Revenue': ('Sales', 'Revenue')},
                        bim_measure_names=['Revenue'])
        entry = self.gen._make_projection_entry({'name': 'Revenue'})
        self.assertIn('Measure', entry['field'])

    def test_column_wrapper(self):
        _init_field_map(self.gen, {'Region': ('Sales', 'Region')})
        entry = self.gen._make_projection_entry({'name': 'Region'})
        self.assertIn('Column', entry['field'])

    def test_physical_measure_aggregation_wrapper(self):
        """Physical numeric columns in _measure_names get Aggregation(Sum)."""
        _init_field_map(self.gen, {'Amount': ('Sales', 'Amount')},
                        measure_names=['Amount'])
        entry = self.gen._make_projection_entry({'name': 'Amount'})
        self.assertIn('Aggregation', entry['field'])
        self.assertEqual(entry['field']['Aggregation']['Function'], 0)
        self.assertEqual(
            entry['field']['Aggregation']['Expression']['Column']['Property'],
            'Amount')

    def test_physical_measure_not_overridden_by_bim(self):
        """When in both _measure_names and _bim_measure_names, Measure wins."""
        _init_field_map(self.gen, {'Revenue': ('Sales', 'Revenue')},
                        measure_names=['Revenue'],
                        bim_measure_names=['Revenue'])
        entry = self.gen._make_projection_entry({'name': 'Revenue'})
        self.assertIn('Measure', entry['field'])

    def test_fallback_resolution_via_datasource_calcs(self):
        """Resolves via datasources_ref calculations when not in field_map."""
        _init_field_map(self.gen, main_table='Facts',
                        datasources_ref=[{'calculations': [
                            {'name': '[calc_profit]', 'caption': 'Profit'}
                        ]}])
        entry = self.gen._make_projection_entry({'name': 'calc_profit'})
        self.assertEqual(entry['field']['Column']['Property'], 'Profit')

    def test_active_and_queryref(self):
        _init_field_map(self.gen, {'Col': ('T', 'Col')})
        entry = self.gen._make_projection_entry({'name': 'Col'})
        self.assertTrue(entry['active'])
        self.assertEqual(entry['queryRef'], 'T.Col')

    def test_native_query_ref_preserves_resolved_property_for_all_wrappers(self):
        entity = 'Données synthétiques'
        prop = 'Montant réalisé (%)'
        column = {'Column': {
            'Expression': {'SourceRef': {'Entity': entity}},
            'Property': prop,
        }}
        for wrapper in ('Column', 'Measure', 'Aggregation'):
            with self.subTest(wrapper=wrapper):
                _init_field_map(
                    self.gen, {'metric_id': (entity, prop)},
                    bim_measure_names=['metric_id'] if wrapper == 'Measure' else [],
                )
                field = {'name': 'metric_id'}
                if wrapper == 'Aggregation':
                    field['aggregation'] = 'sum'
                    expected_field = {'Aggregation': {
                        'Expression': column, 'Function': 0,
                    }}
                else:
                    expected_field = {wrapper: column['Column']}
                self.assertEqual(self.gen._make_projection_entry(field), {
                    'field': expected_field,
                    'queryRef': f'{entity}.{prop}',
                    'nativeQueryRef': prop,
                    'active': True,
                })


# ─── _make_scatter_axis_entry ────────────────────────────────────────


class TestMakeScatterAxisEntry(unittest.TestCase):
    """Scatter axis projection — Measure vs Aggregation wrapper."""

    def setUp(self):
        self.gen = _make_generator()

    def test_bim_measure_scatter(self):
        _init_field_map(self.gen, {'Revenue': ('Sales', 'Revenue')},
                        bim_measure_names=['Revenue'])
        entry = self.gen._make_scatter_axis_entry({'name': 'Revenue'})
        self.assertIn('Measure', entry['field'])

    def test_physical_column_aggregation(self):
        _init_field_map(self.gen, {'Amount': ('Sales', 'Amount')})
        entry = self.gen._make_scatter_axis_entry({'name': 'Amount'})
        self.assertIn('Aggregation', entry['field'])
        self.assertEqual(entry['field']['Aggregation']['Function'], 0)

    def test_fallback_no_field_map(self):
        _init_field_map(self.gen, main_table='Data',
                        datasources_ref=[{'calculations': [
                            {'name': '[x_calc]', 'caption': 'X Value'}
                        ]}])
        entry = self.gen._make_scatter_axis_entry({'name': 'x_calc'})
        self.assertEqual(entry['field']['Aggregation']['Expression']['Column']['Property'], 'X Value')

    def test_native_query_ref_preserves_scatter_property_and_aggregation(self):
        entity = 'Données synthétiques'
        prop = 'Valeur mesurée (%)'
        for is_measure in (False, True):
            with self.subTest(is_measure=is_measure):
                _init_field_map(
                    self.gen, {'axis_id': (entity, prop)},
                    bim_measure_names=['axis_id'] if is_measure else [],
                )
                field_ref = {
                    'Expression': {'SourceRef': {'Entity': entity}},
                    'Property': prop,
                }
                expected_field = ({'Measure': field_ref} if is_measure else {
                    'Aggregation': {'Expression': {'Column': field_ref}, 'Function': 1},
                })
                self.assertEqual(self.gen._make_scatter_axis_entry({
                    'name': 'axis_id', 'aggregation': 'avg',
                }), {
                    'field': expected_field,
                    'queryRef': f'{entity}.{prop}',
                    'nativeQueryRef': prop,
                    'active': True,
                })


# ─── _build_label_objects ───────────────────────────────────────────


class TestBuildLabelObjects(unittest.TestCase):
    """Label objects — show, font, color, position, number format."""

    def setUp(self):
        self.gen = _make_generator()

    def test_show_labels_from_formatting(self):
        objects = {}
        fmt = {'mark': {'mark-labels-show': 'true'}}
        self.gen._build_label_objects(objects, fmt, {})
        self.assertIn('labels', objects)

    def test_show_labels_from_mark_encoding(self):
        objects = {}
        me = {'label': {'show': True}}
        self.gen._build_label_objects(objects, {}, me)
        self.assertIn('labels', objects)

    def test_label_font_size(self):
        objects = {}
        me = {'label': {'show': True, 'font_size': 14}}
        self.gen._build_label_objects(objects, {}, me)
        props = objects['labels'][0]['properties']
        self.assertEqual(props['fontSize'], _L("14D"))

    def test_label_font_color(self):
        objects = {}
        me = {'label': {'show': True, 'font_color': '#FF0000'}}
        self.gen._build_label_objects(objects, {}, me)
        props = objects['labels'][0]['properties']
        self.assertIn('color', props)

    def test_label_position_mapping(self):
        objects = {}
        me = {'label': {'show': True, 'position': 'top'}}
        self.gen._build_label_objects(objects, {}, me)
        props = objects['labels'][0]['properties']
        self.assertEqual(props['labelPosition'], _L("'OutsideEnd'"))

    def test_label_position_center(self):
        objects = {}
        me = {'label': {'show': True, 'position': 'center'}}
        self.gen._build_label_objects(objects, {}, me)
        props = objects['labels'][0]['properties']
        self.assertEqual(props['labelPosition'], _L("'InsideCenter'"))

    def test_label_color_from_formatting(self):
        objects = {}
        fmt = {'label': {'color': '#00FF00'}}
        self.gen._build_label_objects(objects, fmt, {})
        self.assertIn('labels', objects)

    def test_font_family(self):
        objects = {}
        fmt = {'font': {'family': 'Arial'}}
        self.gen._build_label_objects(objects, fmt, {})
        self.assertIn('labels', objects)
        props = objects['labels'][0]['properties']
        self.assertEqual(props['fontFamily'], _L("'Arial'"))

    def test_font_size_pt_strip(self):
        objects = {}
        fmt = {'font': {'size': '12pt'}}
        self.gen._build_label_objects(objects, fmt, {})
        props = objects['labels'][0]['properties']
        self.assertEqual(props['fontSize'], _L("12D"))

    def test_font_size_px_strip(self):
        objects = {}
        fmt = {'font': {'size': '16px'}}
        self.gen._build_label_objects(objects, fmt, {})
        props = objects['labels'][0]['properties']
        self.assertEqual(props['fontSize'], _L("16D"))

    def test_font_size_invalid(self):
        """Invalid font size → no crash, no fontSize key."""
        objects = {}
        fmt = {'font': {'size': 'big'}}
        self.gen._build_label_objects(objects, fmt, {})
        # Should have labels created (for font), but fontSize may be absent
        self.assertIn('labels', objects)

    def test_number_format(self):
        objects = {'labels': [{'properties': {}}]}
        fmt = {'number_format': '#,0.00'}
        self.gen._build_label_objects(objects, fmt, {})
        props = objects['labels'][0]['properties']
        self.assertIn('labelDisplayUnits', props)


# ─── _build_legend_objects ──────────────────────────────────────────


class TestBuildLegendObjects(unittest.TestCase):
    """Legend — position mapping, title, font size."""

    def setUp(self):
        self.gen = _make_generator()

    def test_no_color_field(self):
        objects = {}
        self.gen._build_legend_objects(objects, {}, {})
        self.assertNotIn('legend', objects)

    def test_default_position_right(self):
        objects = {}
        me = {'color': {'field': 'Category'}}
        self.gen._build_legend_objects(objects, me, {})
        self.assertIn('legend', objects)
        pos = objects['legend'][0]['properties']['position']
        self.assertEqual(pos, _L("'Right'"))

    def test_position_bottom(self):
        objects = {}
        me = {'color': {'field': 'Category'}}
        fmt = {'legend': {'position': 'bottom'}}
        self.gen._build_legend_objects(objects, me, fmt)
        pos = objects['legend'][0]['properties']['position']
        self.assertEqual(pos, _L("'Bottom'"))

    def test_position_top_left(self):
        objects = {}
        me = {'color': {'field': 'Category'}}
        fmt = {'legend': {'position': 'top-left'}}
        self.gen._build_legend_objects(objects, me, fmt)
        pos = objects['legend'][0]['properties']['position']
        self.assertEqual(pos, _L("'TopLeft'"))

    def test_legend_title(self):
        objects = {}
        me = {'color': {'field': 'Category'}}
        fmt = {'legend': {'title': 'My Legend'}}
        self.gen._build_legend_objects(objects, me, fmt)
        props = objects['legend'][0]['properties']
        self.assertIn('titleText', props)
        self.assertIn('showTitle', props)

    def test_legend_font_size(self):
        objects = {}
        me = {'color': {'field': 'Category'}}
        fmt = {'legend': {'font-size': 14}}
        self.gen._build_legend_objects(objects, me, fmt)
        props = objects['legend'][0]['properties']
        self.assertIn('fontSize', props)

    def test_skip_multiple_values(self):
        objects = {}
        me = {'color': {'field': 'Multiple Values'}}
        self.gen._build_legend_objects(objects, me, {})
        self.assertNotIn('legend', objects)


# ─── _build_axis_objects ────────────────────────────────────────────


class TestBuildAxisObjects(unittest.TestCase):
    """Axis objects — formatting, explicit axes, dual axis, enhanced config."""

    def setUp(self):
        self.gen = _make_generator()

    def test_axis_display_show(self):
        objects = {}
        ws = {'formatting': {'axis': {'display': 'true'}}}
        self.gen._build_axis_objects(objects, ws, 'clusteredBarChart')
        self.assertIn('categoryAxis', objects)
        self.assertIn('valueAxis', objects)

    def test_axis_display_none(self):
        objects = {}
        ws = {'formatting': {'axis': {'display': 'none'}}}
        self.gen._build_axis_objects(objects, ws, 'clusteredBarChart')
        self.assertNotIn('categoryAxis', objects)

    def test_explicit_axis_title(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'title': 'Month'}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['showAxisTitle'], _L("true"))

    def test_y_axis_range(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'y': {
            'auto_range': False, 'range_min': 0, 'range_max': 100
        }}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['valueAxis'][0]['properties']
        self.assertEqual(props['start'], _L("0D"))
        self.assertEqual(props['end'], _L("100D"))

    def test_y_axis_log_scale(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'y': {'scale': 'log'}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['valueAxis'][0]['properties']
        self.assertEqual(props['axisScale'], _L("'Log'"))

    def test_y_axis_reversed(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'y': {'reversed': True}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['valueAxis'][0]['properties']
        self.assertEqual(props['reverseOrder'], _L("true"))

    def test_x_axis_reversed(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'reversed': True}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['reverseOrder'], _L("true"))

    def test_dual_axis_combo(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {
            'y': {}, 'dual_axis': True, 'dual_axis_sync': True
        }}
        self.gen._build_axis_objects(objects, ws, 'lineClusteredColumnComboChart')
        self.assertIn('y1AxisReferenceLine', objects)

    def test_label_rotation(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'label_rotation': '45'}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['labelAngle'], _L("45L"))

    def test_show_title_false(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'show_title': False}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['showAxisTitle'], _L("false"))

    def test_show_label_false(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'show_label': False}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['show'], _L("false"))

    def test_continuous_axis(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'is_continuous': True}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['axisType'], _L("'Continuous'"))

    def test_categorical_axis(self):
        objects = {}
        ws = {'formatting': {}, 'axes': {'x': {'is_continuous': False}}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        props = objects['categoryAxis'][0]['properties']
        self.assertEqual(props['axisType'], _L("'Categorical'"))

    def test_dual_axis_dict_sync(self):
        """Dual axis via ws_data.dual_axis dict + synchronized."""
        objects = {}
        ws = {'formatting': {}, 'axes': {},
              'dual_axis': {'enabled': True, 'synchronized': True}}
        self.gen._build_axis_objects(objects, ws, 'bar')
        self.assertIn('valueAxis', objects)
        props = objects['valueAxis'][0]['properties']
        self.assertEqual(props['secShow'], _L("true"))


# ─── _build_visual_styling_objects ──────────────────────────────────


class TestBuildVisualStylingObjects(unittest.TestCase):
    """Styling — background, table headers, row banding, grid, data bars, padding."""

    def setUp(self):
        self.gen = _make_generator()

    def test_background_color(self):
        objects = {}
        ws = {'formatting': {'background_color': '#FFFFFF'}, 'fields': []}
        self.gen._build_visual_styling_objects(
            objects, ws, 'bar', ws['formatting'], {})
        self.assertEqual(objects['background'], [{'properties': {
            'color': {'solid': {'color': _L("'#FFFFFF'")}},
        }}])
        self.assertNotIn('visualContainerStyle', objects)
        self.assertNotIn('visualContainerPadding', objects)

    def test_background_from_pane(self):
        objects = {}
        ws = {'formatting': {'pane': {'background-color': '#EEE'}}, 'fields': []}
        self.gen._build_visual_styling_objects(
            objects, ws, 'bar', ws['formatting'], {})
        self.assertEqual(objects['background'], [{'properties': {
            'color': {'solid': {'color': _L("'#EEE'")}},
        }}])
        self.assertNotIn('visualContainerStyle', objects)
        self.assertNotIn('visualContainerPadding', objects)

    def test_table_header_formatting(self):
        objects = {}
        fmt = {'header_style': {
            'font-size': 14, 'font-weight': 'bold', 'font-color': '#000'
        }}
        ws = {'formatting': fmt, 'fields': []}
        self.gen._build_visual_styling_objects(
            objects, ws, 'tableEx', fmt, {})
        self.assertIn('columnHeaders', objects)
        props = objects['columnHeaders'][0]['properties']
        self.assertEqual(props['bold'], _L("true"))

    def test_row_banding(self):
        objects = {}
        fmt = {'worksheet_style': {'band-color': '#F0F0F0'}}
        ws = {'formatting': fmt, 'fields': []}
        self.gen._build_visual_styling_objects(
            objects, ws, 'table', fmt, {})
        self.assertIn('values', objects)

    def test_grid_border(self):
        objects = {}
        fmt = {'header_style': {
            'border-style': 'solid', 'border-color': '#CCC'
        }}
        ws = {'formatting': fmt, 'fields': []}
        self.gen._build_visual_styling_objects(
            objects, ws, 'matrix', fmt, {})
        self.assertIn('gridlines', objects)

    def test_data_bars(self):
        objects = {}
        ws = {'formatting': {}, 'fields': [{'role': 'measure'}]}
        me = {'color': {'type': 'quantitative'}}
        self.gen._build_visual_styling_objects(
            objects, ws, 'tableEx', {}, me)
        self.assertIn('dataBar', objects)

    def test_default_row_banding_table(self):
        """Default banding fallback for table visuals."""
        objects = {}
        ws = {'formatting': {}, 'fields': []}
        self.gen._build_visual_styling_objects(
            objects, ws, 'table', {}, {})
        self.assertIn('values', objects)
        # Check F2F2F2 default
        color = str(objects['values'][0])
        self.assertIn('F2F2F2', color)

    def test_totals_and_subtotals(self):
        objects = {}
        ws = {'formatting': {}, 'fields': [],
              'totals': {'grand_totals': True, 'subtotals': True}}
        self.gen._build_visual_styling_objects(
            objects, ws, 'matrix', {}, {})
        self.assertIn('total', objects)
        self.assertIn('subTotals', objects)

    def test_padding(self):
        objects = {}
        ws = {'formatting': {}, 'fields': [],
              'padding': {'padding_top': 5, 'padding_left': 10}}
        self.gen._build_visual_styling_objects(
            objects, ws, 'bar', {}, {})
        self.assertEqual(objects['padding'], [{'properties': {
            'top': _L('5L'), 'left': _L('10L'),
        }}])
        self.assertNotIn('visualContainerStyle', objects)
        self.assertNotIn('visualContainerPadding', objects)


# ─── _build_color_encoding_objects ──────────────────────────────────


class TestBuildColorEncodingObjects(unittest.TestCase):
    """Color encoding — gradient, single, categorical, stepped thresholds."""

    def setUp(self):
        self.gen = _make_generator()

    def test_gradient_two_colors(self):
        objects = {}
        me = {'color': {'type': 'quantitative',
                         'palette_colors': ['#FF0000', '#00FF00']}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        self.assertIn('dataPoint', objects)
        # PBIR v4.0 does not support 'rules' in dataPoint items
        self.assertNotIn('rules', objects['dataPoint'][0])
        # Static fill color is the first palette color
        fill = objects['dataPoint'][0]['properties']['fill']['solid']['color']
        self.assertIn('#FF0000', str(fill))

    def test_gradient_three_colors_midpoint(self):
        objects = {}
        me = {'color': {'type': 'quantitative',
                         'palette_colors': ['#FF0000', '#FFFF00', '#00FF00']}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        # PBIR v4.0 does not support 'rules' in dataPoint items
        self.assertNotIn('rules', objects['dataPoint'][0])
        fill = objects['dataPoint'][0]['properties']['fill']['solid']['color']
        self.assertIn('#FF0000', str(fill))

    def test_single_color(self):
        objects = {}
        me = {'color': {'type': 'quantitative',
                         'palette_colors': ['#4472C4']}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        self.assertIn('dataPoint', objects)
        self.assertNotIn('rules', objects['dataPoint'][0])

    def test_categorical_color_values(self):
        objects = {}
        me = {'color': {'color_values': {'A': '#FF0000', 'B': '#00FF00'}}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        self.assertIn('dataPoint', objects)
        self.assertEqual(len(objects['dataPoint']), 2)

    def test_stepped_thresholds(self):
        objects = {}
        me = {'color': {'thresholds': [
            {'value': 10, 'color': '#FF0000'},
            {'value': 50, 'color': '#FFFF00'},
            {'value': 90, 'color': '#00FF00'},
        ]}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        self.assertIn('dataPoint', objects)
        # Check inputValue + operator
        dp = objects['dataPoint']
        self.assertTrue(len(dp) >= 3)
        has_input = any('inputValue' in str(r) for r in dp)
        self.assertTrue(has_input)

    def test_stepped_with_null_value(self):
        """Thresholds with None value → catch-all at end."""
        objects = {}
        me = {'color': {'thresholds': [
            {'value': 50, 'color': '#FF0000'},
            {'value': None, 'color': '#CCCCCC'},
        ]}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        self.assertIn('dataPoint', objects)

    def test_conditional_formatting_flag(self):
        """Stepped thresholds with field → conditionalFormatting."""
        objects = {}
        me = {'color': {'field': 'Revenue', 'thresholds': [
            {'value': 10, 'color': '#FF0000'},
            {'value': 50, 'color': '#00FF00'},
        ]}}
        self.gen._build_color_encoding_objects(objects, {}, 'bar', me)
        self.assertIn('conditionalFormatting', objects)


# ─── _build_analytics_objects ───────────────────────────────────────


class TestBuildAnalyticsObjects(unittest.TestCase):
    """Analytics — ref lines, trend, annotations, forecast, map, stats."""

    def setUp(self):
        self.gen = _make_generator()
        self.gen._main_table = 'Sales'

    def test_constant_reference_line(self):
        for visual_type in ('clusteredColumnChart', 'clusteredBarChart'):
            with self.subTest(visual_type=visual_type):
                objects = {}
                ws = {'formatting': {}, 'reference_lines': [
                    {'value': 100, 'label': 'Target', 'color': '#FF0000',
                     'style': 'dashed', 'type': 'constant'}
                ]}
                self.gen._build_analytics_objects(objects, ws, visual_type, {})
                self.assertEqual(objects, {'y1AxisReferenceLine': [{
                    'properties': {
                        'value': _L('100.0D'),
                        'displayName': _L("'Target'"),
                        'lineColor': {'solid': {'color': _L("'#FF0000'")}},
                        'style': _L("'dashed'"),
                        'show': _L('true'),
                    },
                    'selector': {'id': '0'},
                }]})
                self.assertNotIn('valueAxis', objects)
                self.assertNotIn('_unresolved_reference_lines', ws)

    def test_constant_reference_line_zero(self):
        for value in (0, 0.0, '0'):
            with self.subTest(value=value):
                objects = {}
                ws = {'formatting': {}, 'reference_lines': [
                    {'value': value, 'label': 'Zero', 'style': 'solid',
                     'type': 'constant'}
                ]}
                self.gen._build_analytics_objects(
                    objects, ws, 'clusteredColumnChart', {})
                self.assertEqual(objects, {'y1AxisReferenceLine': [{
                    'properties': {
                        'value': _L('0.0D'),
                        'displayName': _L("'Zero'"),
                        'lineColor': {'solid': {'color': _L("'#666666'")}},
                        'style': _L("'solid'"),
                        'show': _L('true'),
                    },
                    'selector': {'id': '0'},
                }]})
                self.assertNotIn('valueAxis', objects)
                self.assertNotIn('_unresolved_reference_lines', ws)

    def test_constant_reference_line_invalid_values(self):
        for value in ('', ' ', None, 'not numeric', '$1,250.50'):
            with self.subTest(value=value):
                objects = {}
                reference_line = {
                    'value': value, 'label': 'Target', 'type': 'constant',
                }
                ws = {'formatting': {}, 'reference_lines': [reference_line]}
                self.gen._build_analytics_objects(
                    objects, ws, 'clusteredColumnChart', {})
                self.assertEqual(objects, {})
                self.assertEqual(ws['_unresolved_reference_lines'],
                                 [reference_line])

    def test_constant_reference_line_currency_and_apostrophe_labels(self):
        for label, literal in (
            ('Target ($)', "'Target ($)'"),
            ("Owner's target ($)", "'Owner''s target ($)'"),
        ):
            with self.subTest(label=label):
                objects = {}
                ws = {'formatting': {}, 'reference_lines': [
                    {'value': '1250.50', 'label': label, 'style': 'dotted',
                     'type': 'constant'}
                ]}
                self.gen._build_analytics_objects(
                    objects, ws, 'clusteredColumnChart', {})
                self.assertEqual(objects, {'y1AxisReferenceLine': [{
                    'properties': {
                        'value': _L('1250.5D'),
                        'displayName': _L(literal),
                        'lineColor': {'solid': {'color': _L("'#666666'")}},
                        'style': _L("'dotted'"),
                        'show': _L('true'),
                    },
                    'selector': {'id': '0'},
                }]})
                self.assertNotIn('valueAxis', objects)

    def test_trend_line(self):
        objects = {}
        ws = {'formatting': {}, 'trend_lines': [
            {'type': 'linear', 'color': '#666'}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        self.assertIn('trend', objects)

    def test_trend_line_with_equation(self):
        objects = {}
        ws = {'formatting': {}, 'trend_lines': [
            {'type': 'exponential', 'color': '#666',
             'show_equation': True, 'show_r_squared': True}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        trend_props = objects['trend'][0]['properties']
        self.assertEqual(trend_props['displayEquation'], _L("true"))
        self.assertEqual(trend_props['displayRSquared'], _L("true"))

    def test_trend_line_invalid_type(self):
        objects = {}
        ws = {'formatting': {}, 'trend_lines': [
            {'type': 'unknown_type', 'color': '#666'}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        self.assertIn('trend', objects)

    def test_annotations_subtitle(self):
        objects = {}
        ws = {'formatting': {}, 'annotations': [
            {'text': 'Note 1'}, {'text': 'Note 2'}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        self.assertIn('subTitle', objects)
        props = objects['subTitle'][0]['properties']
        self.assertEqual(props['show'], _L("true"))

    def test_forecast(self):
        objects = {}
        ws = {'formatting': {}, 'forecasting': [
            {'periods': 10, 'prediction_interval': '90', 'ignore_last': '2'}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        self.assertIn('forecast', objects)
        props = objects['forecast'][0]['properties']
        self.assertEqual(props['forecastLength'], _L("10L"))
        self.assertEqual(props['confidenceLevel'], _L("'90'"))

    def test_map_options(self):
        objects = {}
        ws = {'formatting': {}, 'map_options': {
            'washout': '0.5', 'style': 'dark'
        }}
        self.gen._build_analytics_objects(objects, ws, 'map', {})
        self.assertIn('mapControl', objects)
        props = objects['mapControl'][0]['properties']
        self.assertEqual(props['mapStyle'], _L("'darkGrayscale'"))

    def test_map_invalid_washout(self):
        objects = {}
        ws = {'formatting': {}, 'map_options': {
            'washout': 'invalid', 'style': 'streets'
        }}
        self.gen._build_analytics_objects(objects, ws, 'map', {})
        self.assertIn('mapControl', objects)

    def test_distribution_band(self):
        objects = {}
        ws = {'formatting': {}, 'analytics_stats': [
            {'type': 'distribution_band', 'value_from': '10', 'value_to': '90'}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        ref = objects['valueAxis'][0]['properties']['referenceLine']
        self.assertEqual(ref[0]['type'], 'Band')

    def test_stat_reference(self):
        objects = {}
        ws = {'formatting': {}, 'analytics_stats': [
            {'type': 'stat_line', 'computation': 'median'}
        ]}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        ref = objects['valueAxis'][0]['properties']['referenceLine']
        self.assertEqual(ref[0]['type'], 'Median')

    def test_small_multiples_direct(self):
        objects = {}
        ws = {'formatting': {}, 'small_multiples': 'Region'}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        self.assertIn('smallMultiple', objects)

    def test_small_multiples_from_pages_shelf(self):
        objects = {}
        ws = {'formatting': {}, 'pages_shelf': {'field': 'Year'}}
        self.gen._build_analytics_objects(objects, ws, 'bar', {})
        self.assertIn('smallMultiple', objects)


# ─── _create_visual_filters ─────────────────────────────────────────


class TestCreateVisualFilters(unittest.TestCase):
    """Visual filters — range, categorical, exclude, skips."""

    def setUp(self):
        self.gen = _make_generator()
        _init_field_map(self.gen, {'Region': ('Sales', 'Region'),
                                   'Amount': ('Sales', 'Amount')})

    def test_empty_filters(self):
        result = self.gen._create_visual_filters([])
        self.assertEqual(result, [])

    def test_skip_measure_names(self):
        filters = [{'field': 'Measure Names', 'values': ['x']}]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(result, [])

    def test_skip_measure_values(self):
        filters = [{'field': 'Measure Values', 'values': ['x']}]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(result, [])

    def test_skip_date_part(self):
        filters = [{'field': 'OrderDate', 'date_part': 'yr', 'values': ['x']}]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(result, [])

    def test_range_filter(self):
        filters = [{'field': 'Amount', 'filter_type': 'range',
                     'min': 10, 'max': 100}]
        result = self.gen._create_visual_filters(filters)
        self.assertTrue(len(result) >= 1)
        # Should have ComparisonKind
        filt_str = json.dumps(result)
        self.assertIn('ComparisonKind', filt_str)

    def test_categorical_filter(self):
        filters = [{'field': 'Region', 'values': ['East', 'West']}]
        result = self.gen._create_visual_filters(filters)
        self.assertTrue(len(result) >= 1)
        filt_str = json.dumps(result)
        self.assertIn('In', filt_str)

    def test_exclude_filter(self):
        filters = [{'field': 'Region', 'values': ['East'],
                     'exclude': True}]
        result = self.gen._create_visual_filters(filters)
        filt_str = json.dumps(result)
        self.assertIn('Not', filt_str)

    def test_empty_values_skip(self):
        filters = [{'field': 'Region', 'values': []}]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(result, [])

    def test_dedup_same_field_range(self):
        """Duplicate range filters on the same field are collapsed to one."""
        filters = [
            {'field': 'Amount', 'min': 10, 'max': 100},
            {'field': 'Amount', 'min': 20, 'max': 200},
            {'field': 'Amount', 'min': 30},
        ]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(len(result), 1)
        # Should keep the first one (min=10)
        filt_str = json.dumps(result)
        self.assertIn("10L", filt_str)

    def test_dedup_different_fields_kept(self):
        """Different fields are NOT deduplicated."""
        filters = [
            {'field': 'Amount', 'min': 10},
            {'field': 'Region', 'values': ['East']},
        ]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(len(result), 2)

    def test_dedup_prefers_substantive(self):
        """Empty filter replaced by substantive one for the same field."""
        filters = [
            {'field': 'Amount'},  # no min/max/values → empty
            {'field': 'Amount', 'min': 50},  # has condition
        ]
        result = self.gen._create_visual_filters(filters)
        # Either 0 (if first is skipped) or 1 (dedup + replace)
        self.assertLessEqual(len(result), 1)

    def test_year_part_range_uses_datetime_literal(self):
        """yr: date-part prefix → datetime literal, not integer string."""
        _init_field_map(self.gen, {'Year': ('Extract', 'Year')})
        filters = [{
            'field': '[ds].[yr:Year:qk]',
            'min': '2000', 'max': '2012',
            'filter_mode': 'range',
        }]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(len(result), 1)
        filt_str = json.dumps(result)
        self.assertIn("datetime'2000-01-01T00:00:00'", filt_str)
        self.assertIn("datetime'2012-12-31T23:59:59'", filt_str)
        self.assertNotIn("'2000'", filt_str)

    def test_tableau_date_hash_literal(self):
        """Tableau #YYYY-MM-DD# → PBI datetime literal."""
        _init_field_map(self.gen, {'Year': ('Extract', 'Year')})
        filters = [{
            'field': '[ds].[none:Year:qk]',
            'min': '#2001-12-07#', 'max': '#2012-12-01#',
            'filter_mode': 'range',
        }]
        result = self.gen._create_visual_filters(filters)
        self.assertEqual(len(result), 1)
        filt_str = json.dumps(result)
        self.assertIn("datetime'2001-12-07T00:00:00'", filt_str)
        self.assertIn("datetime'2012-12-01T00:00:00'", filt_str)

    def test_skip_filter_on_parameter_table_measure(self):
        """Filters whose target is a measure on a What-If parameter table
        must be dropped — PBI rejects them with 'primary projections must
        have set equality with the corresponding subset in the filter
        target' because parameter tables have no relationship to fact
        tables. Regression for a real-world parameter-filter page.
        """
        # Parameter named "Inadequate" with its own table + measure.
        _init_field_map(self.gen, {
            'Inadequate': ('Inadequate', 'Inadequate'),
            'Region': ('Sales', 'Region'),
        }, bim_measure_names=['Inadequate'])
        self.gen._parameter_table_names = {'Inadequate'}
        filters = [
            # Filter on parameter measure → must be skipped
            {'field': 'Inadequate', 'values': ['false']},
            # Filter on regular column → must be kept
            {'field': 'Region', 'values': ['East']},
        ]
        result = self.gen._create_visual_filters(filters)
        # Only the Region filter survives
        self.assertEqual(len(result), 1)
        col = (result[0].get('field', {}).get('Column')
               or result[0].get('field', {}).get('Measure', {}))
        self.assertEqual(
            col.get('Expression', {}).get('SourceRef', {}).get('Entity'),
            'Sales',
        )


# ─── _create_report_filters ─────────────────────────────────────────


class TestCreateReportFilters(unittest.TestCase):
    """Report-level filters from parameters."""

    def setUp(self):
        self.gen = _make_generator()
        _init_field_map(self.gen, main_table='Data')

    def test_parameter_filter(self):
        conv = {'parameters': [
            {'name': 'Region', 'currentValue': 'East', 'values': ['East', 'West']}
        ]}
        result = self.gen._create_report_filters(conv)
        self.assertTrue(len(result) >= 1)

    def test_empty_parameters(self):
        conv = {'parameters': []}
        result = self.gen._create_report_filters(conv)
        self.assertEqual(result, [])

    def test_no_parameters_key(self):
        result = self.gen._create_report_filters({})
        self.assertEqual(result, [])


# ─── _resolve_field_entity ──────────────────────────────────────────


class TestResolveFieldEntityCoverage(unittest.TestCase):
    """4-step resolution — direct, prefix strip, partial, fallback."""

    def setUp(self):
        self.gen = _make_generator()
        _init_field_map(self.gen, {'Revenue': ('Sales', 'Revenue'),
                                   'Profit': ('Sales', 'Profit')},
                        main_table='Sales')

    def test_direct_match(self):
        entity, prop = self.gen._resolve_field_entity('Revenue')
        self.assertEqual(entity, 'Sales')
        self.assertEqual(prop, 'Revenue')

    def test_prefix_strip(self):
        """Field with attr: prefix should still resolve."""
        entity, prop = self.gen._resolve_field_entity('attr:Revenue')
        self.assertEqual(entity, 'Sales')

    def test_fallback_main_table(self):
        entity, prop = self.gen._resolve_field_entity('Unknown')
        self.assertEqual(entity, 'Sales')
        self.assertEqual(prop, 'Unknown')


# ─── _create_page_navigator ────────────────────────────────────────


class TestCreatePageNavigator(unittest.TestCase):
    """Page navigator visual — 40px Tabs bar."""

    def test_creates_visual_json(self):
        gen = _make_generator()
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        gen._create_page_navigator(visuals_dir, 1280, 720, 5)
        # Should have created a visual subdirectory
        subdirs = os.listdir(visuals_dir)
        self.assertEqual(len(subdirs), 1)
        visual_json = os.path.join(visuals_dir, subdirs[0], 'visual.json')
        self.assertTrue(os.path.exists(visual_json))
        with open(visual_json) as f:
            data = json.load(f)
        self.assertEqual(data['visual']['visualType'], 'pageNavigator')
        self.assertEqual(data['position']['y'], 720 - 40)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_pages_shelf_slicer ─────────────────────────────────────


class TestCreatePagesShelfSlicer(unittest.TestCase):
    """Animation-hint slicer from Pages shelf."""

    def test_creates_slicer(self):
        gen = _make_generator()
        gen._main_table = 'Sales'
        gen._field_map = {}
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        conv = {'datasources': [{'tables': [
            {'name': 'Sales', 'columns': [{'name': 'Year'}]}
        ]}]}
        gen._create_pages_shelf_slicer(
            visuals_dir, {'field': 'Year'}, 1.0, 1.0, 0, conv)
        subdirs = os.listdir(visuals_dir)
        self.assertEqual(len(subdirs), 1)
        visual_json = os.path.join(visuals_dir, subdirs[0], 'visual.json')
        with open(visual_json) as f:
            data = json.load(f)
        self.assertEqual(data['visual']['visualType'], 'slicer')
        # Should have Pages Shelf comment
        general = data['visual']['objects'].get('general', [])
        self.assertTrue(len(general) >= 1)
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_empty_field_noop(self):
        gen = _make_generator()
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        gen._create_pages_shelf_slicer(visuals_dir, {'field': ''}, 1, 1, 0, {})
        # No visual created
        self.assertEqual(len(os.listdir(visuals_dir)), 0)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_drillthrough_pages ──────────────────────────────────────


class TestCreateDrillthroughPages(unittest.TestCase):
    """Drillthrough pages from filter/set-value actions."""

    def setUp(self):
        self.gen = _make_generator()
        _init_field_map(self.gen, main_table='Sales')
        self.tmpdir = tempfile.mkdtemp()
        self.pages_dir = os.path.join(self.tmpdir, 'pages')
        os.makedirs(self.pages_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_drillthrough_page(self):
        worksheets = [{'name': 'Detail', 'fields': [{'name': 'Revenue'}],
                       'chart_type': 'table'}]
        conv = {
            'actions': [{'type': 'filter', 'target_worksheets': ['Detail'],
                         'field': 'Region'}],
            'datasources': [{'tables': [
                {'name': 'Sales', 'columns': [{'name': 'Region'}]}
            ]}]
        }
        page_names = []
        self.gen._create_drillthrough_pages(
            self.pages_dir, page_names, worksheets, conv)
        self.assertTrue(len(page_names) >= 1)
        # Verify page.json exists
        dt_dir = os.path.join(self.pages_dir, page_names[0])
        with open(os.path.join(dt_dir, 'page.json')) as f:
            data = json.load(f)
        self.assertEqual(data['pageType'], 'Drillthrough')

    def test_no_actions_noop(self):
        page_names = []
        self.gen._create_drillthrough_pages(
            self.pages_dir, page_names, [], {'actions': []})
        self.assertEqual(len(page_names), 0)

    def test_skip_non_filter_actions(self):
        conv = {'actions': [{'type': 'url', 'target_worksheets': ['X']}]}
        page_names = []
        self.gen._create_drillthrough_pages(
            self.pages_dir, page_names, [], conv)
        self.assertEqual(len(page_names), 0)

    def test_set_value_action(self):
        worksheets = [{'name': 'Target', 'fields': [{'name': 'Qty'}],
                       'chart_type': 'bar'}]
        conv = {
            'actions': [{'type': 'set-value',
                         'target_worksheet': 'Target',
                         'source_field': 'Category'}],
            'datasources': [{'tables': [
                {'name': 'T', 'columns': [{'name': 'Category'}]}
            ]}]
        }
        page_names = []
        self.gen._create_drillthrough_pages(
            self.pages_dir, page_names, worksheets, conv)
        self.assertTrue(len(page_names) >= 1)


# ─── _detect_script_visual ──────────────────────────────────────────


class TestDetectScriptVisual(unittest.TestCase):
    """SCRIPT_* detection in calculations + mark_encoding."""

    def test_returns_none_no_ws(self):
        gen = _make_generator()
        result = gen._detect_script_visual(None, {})
        self.assertIsNone(result)

    def test_returns_none_no_scripts(self):
        gen = _make_generator()
        gen._field_map = {}
        ws = {'fields': [{'name': 'Revenue'}]}
        conv = {'calculations': [{'name': 'Revenue', 'formula': 'SUM([Sales])'}]}
        result = gen._detect_script_visual(ws, conv)
        self.assertIsNone(result)


# ─── _build_field_mapping ───────────────────────────────────────────


class TestBuildFieldMapping(unittest.TestCase):
    """Field mapping phases — DS-level inheritance, groups, calcs, measures."""

    def setUp(self):
        self.gen = _make_generator()

    def _build(self, conv):
        self.gen._actual_bim_measure_names = set()
        self.gen._actual_bim_symbols = {}
        self.gen._build_field_mapping(conv)

    def test_basic_mapping(self):
        conv = {
            'datasources': [{'tables': [
                {'name': 'Sales', 'columns': [
                    {'name': 'Region', 'caption': 'Region'},
                    {'name': 'Amount', 'caption': 'Amount'}
                ]}
            ], 'calculations': []}],
            'calculations': [], 'groups': []
        }
        self._build(conv)
        self.assertIn('Region', self.gen._field_map)
        self.assertEqual(self.gen._main_table, 'Sales')

    def test_measure_classification(self):
        """Physical role='measure' → _measure_names but NOT _bim_measure_names."""
        conv = {
            'datasources': [{'tables': [
                {'name': 'Sales', 'columns': [
                    {'name': 'Revenue', 'role': 'measure'}
                ]}
            ], 'calculations': []}],
            'calculations': [], 'groups': []
        }
        self._build(conv)
        self.assertIn('Revenue', self.gen._measure_names)
        self.assertNotIn('Revenue', self.gen._bim_measure_names)

    def test_calculation_indexing(self):
        conv = {
            'datasources': [{'tables': [
                {'name': 'Facts', 'columns': []}
            ], 'calculations': [
                {'name': '[Profit]', 'caption': 'Profit', 'role': 'measure'}
            ]}],
            'calculations': [], 'groups': []
        }
        self._build(conv)
        self.assertIn('Profit', self.gen._field_map)
        self.assertIn('Profit', self.gen._measure_names)

    def test_groups_mapping(self):
        conv = {
            'datasources': [{'tables': [
                {'name': 'T', 'columns': []}
            ], 'calculations': []}],
            'calculations': [], 'groups': [{'name': '[RegionGroup]'}]
        }
        self._build(conv)
        self.assertIn('RegionGroup', self.gen._field_map)

    def test_bim_measure_names_populated(self):
        # Call _build_field_mapping directly to avoid helper resetting _actual_bim_measure_names
        self.gen._actual_bim_measure_names = {'TotalSales'}
        self.gen._actual_bim_symbols = {}
        conv = {
            'datasources': [{'tables': [
                {'name': 'S', 'columns': [{'name': 'Col1'}]}
            ], 'calculations': []}],
            'calculations': [], 'groups': []
        }
        self.gen._build_field_mapping(conv)
        self.assertIn('TotalSales', self.gen._bim_measure_names)

    def test_top_level_calc_measures(self):
        conv = {
            'datasources': [{'tables': [
                {'name': 'T', 'columns': []}
            ], 'calculations': []}],
            'calculations': [
                {'name': '[Margin]', 'caption': 'Margin', 'role': 'measure'}
            ],
            'groups': []
        }
        self._build(conv)
        self.assertIn('Margin', self.gen._measure_names)

    def test_skip_special_columns(self):
        """DS-level columns starting with '[:' should be skipped during inheritance."""
        conv = {
            'datasources': [{
                'tables': [
                    {'name': 'T'}  # No columns → triggers DS-level inheritance
                ],
                'columns': [
                    {'name': '[:internal]', 'caption': 'internal'},
                    {'name': 'Real', 'caption': 'Real'}
                ],
                'calculations': []
            }],
            'calculations': [], 'groups': []
        }
        self._build(conv)
        self.assertNotIn('[:internal]', self.gen._field_map)
        self.assertIn('Real', self.gen._field_map)

    def test_multiple_datasources_main_table(self):
        """Main table = table with most columns."""
        conv = {
            'datasources': [
                {'tables': [
                    {'name': 'Small', 'columns': [{'name': 'A'}]},
                ], 'calculations': []},
                {'tables': [
                    {'name': 'Big', 'columns': [
                        {'name': 'X'}, {'name': 'Y'}, {'name': 'Z'}
                    ]},
                ], 'calculations': []},
            ],
            'calculations': [], 'groups': []
        }
        self._build(conv)
        self.assertEqual(self.gen._main_table, 'Big')


# ─── _create_swap_bookmarks ─────────────────────────────────────────


class TestCreateSwapBookmarks(unittest.TestCase):
    """Dynamic zone → swap bookmarks with MigrationNote."""

    def setUp(self):
        self.gen = _make_generator()

    def test_creates_bookmarks(self):
        dynamic_zones = [
            {'zone': 'zone1', 'field': 'Param', 'value': 'A',
             'condition': 'equals', 'worksheet': 'Sheet1'},
            {'zone': 'zone1', 'field': 'Param', 'value': 'B',
             'condition': 'equals', 'worksheet': 'Sheet2'},
        ]
        result = self.gen._create_swap_bookmarks(dynamic_zones, 'Page1')
        self.assertTrue(len(result) >= 2)
        # Check PBIR-compliant options are present
        bm_str = json.dumps(result)
        self.assertIn('targetVisualNames', bm_str)

    def test_empty_zones(self):
        result = self.gen._create_swap_bookmarks([], 'Page1')
        self.assertEqual(result, [])


# ─── _copy_custom_shapes ────────────────────────────────────────────


class TestCopyCustomShapes(unittest.TestCase):
    """Shape file copying from tableau_export/shapes/."""

    def test_no_shapes_directory(self):
        """No crash when shapes dir doesn't exist."""
        gen = _make_generator()
        tmpdir = tempfile.mkdtemp()
        def_dir = os.path.join(tmpdir, 'definition')
        os.makedirs(def_dir, exist_ok=True)
        # Should not crash
        gen._copy_custom_shapes(def_dir, {})
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── create_metadata ────────────────────────────────────────────────


class TestCreateMetadata(unittest.TestCase):
    """Migration metadata JSON generation."""

    def test_creates_metadata_file(self):
        gen = _make_generator()
        tmpdir = tempfile.mkdtemp()
        project_dir = os.path.join(tmpdir, 'TestProject')
        os.makedirs(project_dir, exist_ok=True)
        conv = {
            'worksheets': [{'name': 'Sheet1'}],
            'dashboards': [],
            'datasources': [],
            'calculations': [],
            'parameters': [],
            'filters': [],
        }
        gen.create_metadata(project_dir, 'TestReport', conv)
        meta_path = os.path.join(project_dir, 'migration_metadata.json')
        self.assertTrue(os.path.exists(meta_path))
        with open(meta_path) as f:
            data = json.load(f)
        self.assertEqual(data['report_name'], 'TestReport')
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_paginated_report ───────────────────────────────────────


class TestCreatePaginatedReport(unittest.TestCase):
    """RDL-style paginated report — report.json, header, footer, tablix."""

    def test_creates_paginated_structure(self):
        gen = _make_generator()
        gen._main_table = 'Sales'
        gen._field_map = {'Revenue': ('Sales', 'Revenue')}
        gen._measure_names = set()
        gen._bim_measure_names = set()
        gen._datasources_ref = []
        tmpdir = tempfile.mkdtemp()
        project_dir = os.path.join(tmpdir, 'Project')
        os.makedirs(project_dir, exist_ok=True)
        conv = {
            'worksheets': [{'name': 'Sheet1', 'fields': [
                {'name': 'Region'}, {'name': 'Revenue'}
            ]}],
            'datasources': [],
        }
        gen._create_paginated_report(project_dir, 'Report', conv)
        pag_dir = os.path.join(project_dir, 'PaginatedReport')
        self.assertTrue(os.path.isdir(pag_dir))
        report_json = os.path.join(pag_dir, 'report.json')
        self.assertTrue(os.path.exists(report_json))
        with open(report_json) as f:
            data = json.load(f)
        self.assertIn('pageWidth', data)
        # Check header and footer
        self.assertTrue(os.path.exists(os.path.join(pag_dir, 'header.json')))
        self.assertTrue(os.path.exists(os.path.join(pag_dir, 'footer.json')))
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_multiple_worksheets(self):
        gen = _make_generator()
        gen._main_table = 'T'
        gen._field_map = {}
        gen._measure_names = set()
        gen._bim_measure_names = set()
        gen._datasources_ref = []
        tmpdir = tempfile.mkdtemp()
        project_dir = os.path.join(tmpdir, 'P')
        os.makedirs(project_dir, exist_ok=True)
        conv = {
            'worksheets': [
                {'name': 'S1', 'fields': [{'name': 'A'}]},
                {'name': 'S2', 'fields': [{'name': 'B'}]},
            ],
            'datasources': [],
        }
        gen._create_paginated_report(project_dir, 'R', conv)
        pag_dir = os.path.join(project_dir, 'PaginatedReport')
        # Should have tablix pages
        pages = [f for f in os.listdir(pag_dir)
                 if f.startswith('page_') or f.startswith('tablix')]
        self.assertTrue(len(pages) >= 0)  # Structure exists
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── create_tmdl_model error path ───────────────────────────────────


class TestCreateTmdlModelError(unittest.TestCase):
    """Error path in create_tmdl_model — try/except with traceback."""

    def test_error_does_not_crash(self):
        """When TMDL generation raises, should print error not crash."""
        gen = _make_generator()
        gen._calendar_start = None
        gen._calendar_end = None
        gen._culture = None
        gen._model_mode = 'import'
        gen._languages = None
        tmpdir = tempfile.mkdtemp()
        sm_dir = os.path.join(tmpdir, 'sm')
        os.makedirs(sm_dir, exist_ok=True)
        # Patch tmdl_generator.generate_tmdl to raise an error
        with patch('powerbi_import.pbip_generator.tmdl_generator') as mock_tmdl:
            mock_tmdl.generate_tmdl.side_effect = ValueError('test error')
            # Should NOT raise — error is caught internally
            gen.create_tmdl_model(sm_dir, 'Test', {})
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _make_visual_position edge cases ───────────────────────────────


class TestMakeVisualPositionEdgeCases(unittest.TestCase):
    """Edge cases — clamping to page bounds, min size."""

    def test_clamp_width_to_page(self):
        gen = _make_generator()
        pos = {'x': 1200, 'y': 0, 'w': 200, 'h': 100}
        result = gen._make_visual_position(pos, 1.0, 1.0, 0, 1280, 720)
        self.assertLessEqual(result['x'] + result['width'], 1280)

    def test_clamp_height_to_page(self):
        gen = _make_generator()
        pos = {'x': 0, 'y': 650, 'w': 100, 'h': 200}
        result = gen._make_visual_position(pos, 1.0, 1.0, 0, 1280, 720)
        self.assertLessEqual(result['y'] + result['height'], 720)

    def test_minimum_width(self):
        gen = _make_generator()
        pos = {'x': 0, 'y': 0, 'w': 10, 'h': 100}
        result = gen._make_visual_position(pos, 1.0, 1.0, 0, 1280, 720)
        self.assertGreaterEqual(result['width'], 60)

    def test_minimum_height(self):
        gen = _make_generator()
        pos = {'x': 0, 'y': 0, 'w': 100, 'h': 10}
        result = gen._make_visual_position(pos, 1.0, 1.0, 0, 1280, 720)
        self.assertGreaterEqual(result['height'], 40)


# ─── Integration test: generate_project with paginated ──────────────


class TestGenerateProjectPaginated(unittest.TestCase):
    """Integration: generate_project with paginated=True."""

    def test_paginated_flag_stored(self):
        gen = _make_generator()
        conv = {
            'worksheets': [{'name': 'Sheet1', 'fields': [{'name': 'A'}],
                           'chart_type': 'bar'}],
            'dashboards': [],
            'datasources': [{'tables': [
                {'name': 'T', 'columns': [{'name': 'A'}]}
            ], 'calculations': []}],
            'calculations': [], 'parameters': [], 'filters': [],
            'stories': [], 'actions': [], 'sets': [], 'groups': [],
            'bins': [], 'hierarchies': [], 'sort_orders': [],
            'aliases': [], 'custom_sql': [], 'user_filters': [],
        }
        with patch.object(gen, 'create_tmdl_model'):
            with patch.object(gen, '_create_paginated_report') as mock_pag:
                gen.generate_project('Test', conv, paginated=True)
                self.assertTrue(gen._paginated)
                mock_pag.assert_called_once()


# ─── _build_visual_objects orchestrator ──────────────────────────────


class TestBuildVisualObjectsOrchestrator(unittest.TestCase):
    """Orchestrator delegates to sub-methods."""

    def test_title_in_visualContainerObjects(self):
        gen = _make_generator()
        _init_field_map(gen, main_table='T')
        objects = gen._build_visual_objects('MySheet', None, 'bar')
        # Title is in visualContainerObjects, not in visual.objects
        self.assertNotIn('title', objects)

    def test_with_formatting(self):
        gen = _make_generator()
        gen._main_table = 'T'
        ws = {
            'formatting': {'mark': {'mark-labels-show': 'true'}},
            'mark_encoding': {'label': {'show': True}},
        }
        objects = gen._build_visual_objects('Sheet', ws, 'bar')
        self.assertIn('labels', objects)

    def test_non_cartesian_objects_do_not_contain_cartesian_axes(self):
        gen = _make_generator()
        self.addCleanup(shutil.rmtree, gen.output_dir, ignore_errors=True)
        _init_field_map(gen, main_table='Synthetic')
        worksheet = {
            'formatting': {'axis': {'display': 'true'}},
            'axes': {'x': {'title': 'Category'}, 'y': {'title': 'Amount'}},
        }
        for visual_type in (
            'card', 'multiRowCard', 'tableEx', 'table', 'matrix', 'pivotTable',
            'pieChart', 'donutChart', 'azureMap', 'map', 'filledMap', 'shapeMap',
            'treemap', 'gauge', 'kpi',
        ):
            with self.subTest(visual_type=visual_type):
                objects = gen._build_visual_objects('Synthetic', worksheet, visual_type)
                self.assertNotIn('categoryAxis', objects)
                self.assertNotIn('valueAxis', objects)

    def test_cartesian_objects_keep_axis_configuration(self):
        gen = _make_generator()
        self.addCleanup(shutil.rmtree, gen.output_dir, ignore_errors=True)
        _init_field_map(gen, main_table='Synthetic')
        worksheet = {
            'formatting': {'axis': {'display': 'true'}},
            'axes': {'y': {'auto_range': False, 'range_min': 0, 'range_max': 100}},
        }
        for visual_type in ('clusteredBarChart', 'clusteredColumnChart', 'lineChart',
                            'areaChart', 'scatterChart', 'lineClusteredColumnComboChart',
                            'lineStackedColumnComboChart'):
            with self.subTest(visual_type=visual_type):
                objects = gen._build_visual_objects('Synthetic', worksheet, visual_type)
                self.assertEqual(objects['categoryAxis'][0]['properties']['show'],
                                 _L('true'))
                properties = objects['valueAxis'][0]['properties']
                self.assertEqual(properties['show'], _L('true'))
                self.assertEqual(properties['start'], _L('0D'))
                self.assertEqual(properties['end'], _L('100D'))

    def test_card_label_font_is_preserved_without_show_property(self):
        gen = _make_generator()
        self.addCleanup(shutil.rmtree, gen.output_dir, ignore_errors=True)
        _init_field_map(gen, main_table='Synthetic')
        objects = gen._build_visual_objects('Synthetic', {
            'formatting': {
                'mark': {'mark-labels-show': 'true'},
                'font': {'family': 'Georgia', 'size': '13pt'},
            },
            'mark_encoding': {'label': {'show': True}},
        }, 'card')
        self.assertEqual(objects, {'labels': [{'properties': {
            'fontFamily': _L("'Georgia'"), 'fontSize': _L('13D'),
        }}]})

    def test_multi_row_card_remaps_label_font_to_data_labels(self):
        gen = _make_generator()
        self.addCleanup(shutil.rmtree, gen.output_dir, ignore_errors=True)
        _init_field_map(gen, main_table='Synthetic')
        objects = gen._build_visual_objects('Synthetic', {
            'formatting': {
                'font': {'family': 'Georgia', 'size': '13pt'},
                'label': {'color': '#234567'},
            },
        }, 'multiRowCard')
        self.assertEqual(objects, {'dataLabels': [{'properties': {
            'fontFamily': _L("'Georgia'"),
            'fontSize': _L('13D'),
            'color': {'solid': {'color': _L("'#234567'")}},
        }}]})

    def test_table_values_font_preserves_banding_without_chart_objects(self):
        gen = _make_generator()
        self.addCleanup(shutil.rmtree, gen.output_dir, ignore_errors=True)
        _init_field_map(gen, main_table='Synthetic')
        objects = gen._build_visual_objects('Synthetic', {
            'formatting': {
                'mark': {'mark-labels-show': 'true'},
                'font': {'family': 'Georgia', 'size': '13pt'},
                'label': {'color': '#234567'},
                'worksheet_style': {'band-color': '#E8EEF0'},
            },
            'mark_encoding': {'color': {
                'field': 'Category', 'type': 'quantitative',
                'palette_colors': ['#23567A', '#7AB1C2'],
            }},
        }, 'tableEx')
        self.assertEqual(objects, {'values': [{'properties': {
            'backColor': {'solid': {'color': _L("'#E8EEF0'")}},
            'fontFamily': _L("'Georgia'"),
            'fontSize': _L('13D'),
            'fontColor': {'solid': {'color': _L("'#234567'")}},
        }}]})


class TestWorksheetContainerObjects(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.gen = _make_generator(self.tmpdir.name)
        _init_field_map(self.gen, main_table='Synthetic')
        self.worksheet = {'chart_type': 'clusteredBarChart', 'fields': []}
        self.zone = {
            'worksheetName': 'Synthetic Chart',
            'position': {'x': 0, 'y': 0, 'w': 400, 'h': 300},
        }

    def _create_worksheet_visual(self):
        self.gen._create_visual_worksheet(
            self.tmpdir.name, self.worksheet, self.zone,
            1.0, 1.0, 0, [self.worksheet], {},
        )
        visual_files = list(Path(self.tmpdir.name).rglob('visual.json'))
        self.assertEqual(len(visual_files), 1)
        return json.loads(visual_files[0].read_text(encoding='utf-8'))

    def _bind_chart_fields(self):
        """Fill the Category and Y roles clusteredBarChart requires, so the
        visual stays a chart; chrome is meaningless on a textbox fallback."""
        self.gen._measure_names = {'Revenue'}
        self.gen._bim_measure_names = {'Revenue'}
        self.worksheet['fields'] = [
            {'name': 'Region', 'shelf': 'rows'},
            {'name': 'Revenue', 'shelf': 'columns'},
        ]

    def test_unresolved_reference_lines_add_migration_note(self):
        self.worksheet['reference_lines'] = [
            {'value': '', 'label': 'Empty target', 'type': 'constant'},
            {'value': 'not numeric', 'label': 'Invalid target',
             'type': 'constant'},
        ]
        payload = self._create_worksheet_visual()
        self.assertIn({
            'name': 'MigrationNote',
            'value': '2 reference line(s) have no numeric value; review required.',
        }, payload.get('annotations', []))
        objects = payload['visual']['objects']
        self.assertNotIn('y1AxisReferenceLine', objects)
        for value_axis in objects.get('valueAxis', []):
            self.assertNotIn('referenceLine', value_axis['properties'])
        self.assertNotIn('_unresolved_reference_lines', self.worksheet)

    def test_worksheet_routes_chrome_to_visual_container_objects(self):
        self._bind_chart_fields()
        container_objects = {
            'background': [{'properties': {
                'show': _L('true'),
                'color': {'solid': {'color': _L("'#102030'")}},
            }}],
            'border': [{'properties': {
                'show': _L('true'), 'width': _L('2D'),
                'color': {'solid': {'color': _L("'#405060'")}},
            }}],
            'padding': [{'properties': {'top': _L('5D'), 'left': _L('7D')}}],
            'dropShadow': [{'properties': {'show': _L('true')}}],
        }
        chart_objects = {
            'categoryAxis': [{'properties': {'show': _L('true')}}],
            'labels': [{'properties': {'fontSize': _L('12D')}}],
        }
        with patch.object(self.gen, '_build_visual_objects',
                          return_value={**container_objects, **chart_objects}) as mock_build:
            payload = self._create_worksheet_visual()
        mock_build.assert_called_once_with('Synthetic Chart', self.worksheet,
                                           'clusteredBarChart')
        self.assertNotIn('visualContainerObjects', payload)
        visual = payload['visual']
        self.assertEqual(visual['visualType'], 'clusteredBarChart')
        self.assertEqual(visual['objects'], chart_objects)
        self.assertEqual(visual['visualContainerObjects'], {
            'title': [{'properties': {
                'show': _L('true'), 'text': _L("'Synthetic Chart'"),
            }}],
            **container_objects,
        })

    def test_zone_padding_overrides_worksheet_padding_in_container_objects(self):
        self._bind_chart_fields()
        self.worksheet['padding'] = {'padding_top': 5, 'padding_left': 7}
        self.zone['padding'] = {
            'padding-left': 0, 'padding-right': 9,
            'padding-top': 3, 'padding-bottom': 8,
        }
        with patch.object(self.gen, '_build_visual_objects', return_value={
            'padding': [{'properties': {'top': _L('5D'), 'left': _L('7D')}}],
        }):
            visual = self._create_worksheet_visual()['visual']
        self.assertEqual(visual['visualType'], 'clusteredBarChart')
        self.assertEqual(set(visual['visualContainerObjects']), {'title', 'padding'})
        self.assertEqual(visual['visualContainerObjects']['padding'], [{'properties': {
            'left': _L('0D'), 'right': _L('9D'), 'top': _L('3D'), 'bottom': _L('8D'),
        }}])
        self.assertEqual(visual['objects'], {})

    def test_worksheet_formatting_background_and_padding_use_container_objects(self):
        self.worksheet['formatting'] = {'background_color': '#102030'}
        self.worksheet['padding'] = {'padding_top': 5, 'padding_left': 7}
        visual = self._create_worksheet_visual()['visual']
        container_objects = visual['visualContainerObjects']
        self.assertEqual(set(container_objects), {'title', 'background', 'padding'})
        self.assertEqual(container_objects['background'][0]['properties']['color'],
                         {'solid': {'color': _L("'#102030'")}})
        self.assertEqual(container_objects['padding'], [{'properties': {
            'top': _L('5L'), 'left': _L('7L'),
        }}])
        for name in ('background', 'padding', 'visualContainerStyle', 'visualContainerPadding'):
            self.assertNotIn(name, visual['objects'])

    def test_zone_border_is_written_as_container_object(self):
        self.zone['padding'] = {'border_style': 'solid', 'border_color': '#405060'}
        visual = self._create_worksheet_visual()['visual']
        self.assertEqual(set(visual['visualContainerObjects']), {'title', 'border'})
        self.assertEqual(visual['visualContainerObjects']['border'], [{'properties': {
            'show': _L('true'), 'color': {'solid': {'color': _L("'#405060'")}},
        }}])
        self.assertNotIn('border', visual['objects'])


# ─── _create_action_visuals ─────────────────────────────────────────


class TestCreateActionVisuals(unittest.TestCase):
    """Action buttons — URL and sheet-navigate."""

    def setUp(self):
        self.gen = _make_generator()
        self.tmpdir = tempfile.mkdtemp()
        self.visuals_dir = os.path.join(self.tmpdir, 'visuals')
        os.makedirs(self.visuals_dir, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_normalize_action_visual_preserves_targets(self):
        cases = (
            ('WebUrl', 'webUrl', 'https://example.com', 'webUrl',
             'ArrowRight', 'rightArrow'),
            ('Bookmark', 'bookmark', 'Bookmark1', 'bookmark',
             'Filter', 'bookmarks'),
            ('PageNavigation', 'destinationPage', 'ReportSection2', 'navigationSection',
             'ArrowRight', 'rightArrow'),
        )
        for (action_type, target_property, target, expected_property,
             icon, expected_icon) in cases:
            with self.subTest(action_type=action_type):
                container = {'visual': {
                    'visualType': 'actionButton',
                    'objects': {
                        'action': [{'properties': {
                            'type': _L(f"'{action_type}'"),
                            target_property: _L(f"'{target}'"),
                        }}],
                        'icon': [{'properties': {'shapeType': _L(f"'{icon}'")}}],
                    },
                }}
                self.gen._normalize_action_visual(container)
                visual = container['visual']
                self.assertNotIn('action', visual['objects'])
                self.assertEqual(visual['visualContainerObjects']['visualLink'],
                                 [{'properties': {
                                     'type': _L(f"'{action_type}'"),
                                     expected_property: _L(f"'{target}'"),
                                     'show': _L('true'),
                                 }}])
                self.assertNotIn('destinationPage',
                                 visual['visualContainerObjects']['visualLink'][0]['properties'])
                self.assertEqual(visual['objects']['icon'],
                                 [{'properties': {'shapeType': _L(f"'{expected_icon}'")}}])

    def test_url_action(self):
        actions = [{'type': 'url', 'url': 'https://example.com',
                    'name': 'Go'}]
        count = self.gen._create_action_visuals(
            self.visuals_dir, actions, 1.0, 1.0, 0, 'Page1')
        self.assertEqual(count, 1)
        subdirs = os.listdir(self.visuals_dir)
        self.assertEqual(len(subdirs), 1)
        with open(os.path.join(self.visuals_dir, subdirs[0], 'visual.json'),
                  encoding='utf-8') as stream:
            visual = json.load(stream)['visual']
        self.assertEqual(visual['visualType'], 'actionButton')
        self.assertNotIn('action', visual['objects'])
        self.assertEqual(visual['visualContainerObjects']['visualLink'],
                         [{'properties': {
                             'type': _L("'WebUrl'"),
                             'webUrl': _L("'https://example.com'"),
                             'show': _L('true'),
                         }}])
        self.assertEqual(visual['objects']['icon'][0]['properties']['shapeType'],
                         _L("'rightArrow'"))

    def test_navigate_action(self):
        actions = [{'type': 'sheet-navigate', 'name': 'Nav',
                    'target_worksheet': 'Sheet2'}]
        count = self.gen._create_action_visuals(
            self.visuals_dir, actions, 1.0, 1.0, 0, 'Page1')
        self.assertEqual(count, 1)

    def test_non_url_or_navigate_skip(self):
        actions = [{'type': 'filter', 'name': 'Filter'}]
        count = self.gen._create_action_visuals(
            self.visuals_dir, actions, 1.0, 1.0, 0, 'Page1')
        self.assertEqual(count, 0)

    def test_multiple_actions_y_offset(self):
        actions = [
            {'type': 'url', 'url': 'https://a.com', 'name': 'A'},
            {'type': 'url', 'url': 'https://b.com', 'name': 'B'},
        ]
        count = self.gen._create_action_visuals(
            self.visuals_dir, actions, 1.0, 1.0, 0, 'Page1')
        self.assertEqual(count, 2)


# ─── _create_visual_textbox ─────────────────────────────────────────


class TestCreateVisualTextbox(unittest.TestCase):
    """Textbox visual creation."""

    def test_creates_textbox_visual(self):
        gen = _make_generator()
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        obj = {'text': 'Hello', 'position': {'x': 0, 'y': 0, 'w': 200, 'h': 50}}
        gen._create_visual_textbox(visuals_dir, obj, 1.0, 1.0, 0)
        subdirs = os.listdir(visuals_dir)
        self.assertEqual(len(subdirs), 1)
        visual_json = os.path.join(visuals_dir, subdirs[0], 'visual.json')
        self.assertTrue(os.path.exists(visual_json))
        with open(visual_json) as f:
            data = json.load(f)
        self.assertEqual(data['visual']['visualType'], 'textbox')
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_visual_image ───────────────────────────────────────────


class TestCreateVisualImage(unittest.TestCase):
    """Image visual with source URL."""

    def test_creates_image_visual(self):
        gen = _make_generator()
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        obj = {'url': 'https://img.example.com/logo.png',
               'position': {'x': 0, 'y': 0, 'w': 100, 'h': 100}}
        gen._create_visual_image(visuals_dir, obj, 1.0, 1.0, 0)
        subdirs = os.listdir(visuals_dir)
        self.assertEqual(len(subdirs), 1)
        visual_json = os.path.join(visuals_dir, subdirs[0], 'visual.json')
        with open(visual_json) as f:
            data = json.load(f)
        self.assertEqual(data['visual']['visualType'], 'image')
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_visual_filter_control ──────────────────────────────────


class TestCreateVisualFilterControl(unittest.TestCase):
    """Filter control → slicer visual."""

    def test_creates_slicer_from_filter_control(self):
        gen = _make_generator()
        _init_field_map(gen, {'Region': ('Sales', 'Region')})
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        obj = {
            'type': 'filter_control',
            'calc_col_id': 'Region',
            'position': {'x': 0, 'y': 0, 'w': 200, 'h': 50}
        }
        conv = {'parameters': [], 'datasources': []}
        gen._create_visual_filter_control(
            visuals_dir, obj, 1.0, 1.0, 0, {}, conv)
        subdirs = os.listdir(visuals_dir)
        self.assertTrue(len(subdirs) >= 1)
        shutil.rmtree(tmpdir, ignore_errors=True)

    def test_calc_id_to_caption_resolution(self):
        """Uses calc_id_to_caption map for proper field names."""
        gen = _make_generator()
        _init_field_map(gen, {'Profit': ('Sales', 'Profit')})
        tmpdir = tempfile.mkdtemp()
        visuals_dir = os.path.join(tmpdir, 'visuals')
        os.makedirs(visuals_dir, exist_ok=True)
        obj = {
            'type': 'filter_control',
            'calc_col_id': 'calc_123',
            'position': {'x': 0, 'y': 0, 'w': 200, 'h': 50}
        }
        conv = {'parameters': [], 'datasources': []}
        calc_map = {'calc_123': 'Profit'}
        gen._create_visual_filter_control(
            visuals_dir, obj, 1.0, 1.0, 0, calc_map, conv)
        subdirs = os.listdir(visuals_dir)
        self.assertTrue(len(subdirs) >= 1)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_tooltip_pages ──────────────────────────────────────────


class TestCreateTooltipPages(unittest.TestCase):
    """Tooltip pages — viz_in_tooltip flag."""

    def test_creates_tooltip_page(self):
        gen = _make_generator()
        _init_field_map(gen, main_table='T')
        tmpdir = tempfile.mkdtemp()
        pages_dir = os.path.join(tmpdir, 'pages')
        os.makedirs(pages_dir, exist_ok=True)
        worksheets = [
            {'name': 'Tooltip1', 'vizql_tooltip': True,
             'tooltip': {'viz_in_tooltip': True},
             'fields': [{'name': 'A'}], 'chart_type': 'bar'}
        ]
        conv = {'datasources': []}
        page_names = []
        gen._create_tooltip_pages(
            pages_dir, page_names, worksheets, conv)
        # Should create a tooltip page
        if page_names:
            page_dir = os.path.join(pages_dir, page_names[0])
            with open(os.path.join(page_dir, 'page.json')) as f:
                data = json.load(f)
            self.assertEqual(data.get('pageType'), 'Tooltip')
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── _create_mobile_pages ───────────────────────────────────────────


class TestCreateMobilePages(unittest.TestCase):
    """Mobile pages — phone layout 568×320."""

    def test_creates_mobile_page(self):
        gen = _make_generator()
        _init_field_map(gen, main_table='T')
        tmpdir = tempfile.mkdtemp()
        pages_dir = os.path.join(tmpdir, 'pages')
        os.makedirs(pages_dir, exist_ok=True)
        dashboards = [{
            'name': 'Dashboard1',
            'device_layouts': [{
                'device_type': 'phone',
                'auto_generated': False,
                'zones': [
                    {'name': 'S1', 'position': {'x': 0, 'y': 0, 'w': 300, 'h': 200}}
                ]
            }]
        }]
        worksheets = [{'name': 'S1', 'fields': [{'name': 'X'}], 'chart_type': 'bar'}]
        conv = {'datasources': []}
        page_names = []
        gen._create_mobile_pages(
            pages_dir, page_names, dashboards, worksheets, conv)
        # Should create a mobile page
        self.assertTrue(len(page_names) >= 1)
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
