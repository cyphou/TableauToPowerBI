"""Acceptance regressions for the reported workbook: the model must evaluate.

One fabricated data source reproduces, shape for shape, every calculation of a
production workbook that the engine emitted as ``BLANK()`` or as DAX that
Power BI Desktop rejected at query time. No value or name of that workbook is
used. The source goes through the real extractor and the real model builder.

Power BI is not available here, so each check is the static invariant whose
violation produced a calculation error in Desktop:

* "SUM cannot work with values of type String": a placeholder column that is
  aggregated must be numeric;
* "a single value for column ... cannot be determined": a row iterator may read
  another table's column only when it iterates the joined rows;
* "MIN only accepts a column reference": no ``AGG([measure])`` survives;
* "cannot convert value of type Text": no numeric Formatted companion wraps a
  text measure;
* "a circular dependency was detected" (the project does not open): no
  calculated column calls a measure.

Passing this module is meant to be equivalent to the reported workbook
migrating with no ``BLANK()`` beyond the two documented limits and no
calculation error. The checks state outcomes, not one implementation: any
DAX that satisfies them is accepted.
"""

import re
import unittest
import xml.etree.ElementTree as ET

from powerbi_import.tmdl_generator import _build_semantic_model
from tableau_export.datasource_extractor import extract_datasource

SOURCE = 'AcceptanceSource'
LEFT, RIGHT = 'Housing', 'Households'
FIELDS = {
    LEFT: [('Zone', 'string'), ('Status', 'string'), ('Kind', 'string'),
           ('Sector', 'string'), ('Usage', 'real'), ('Homes', 'integer'), ('Fee', 'real')],
    RIGHT: [('ZoneCode', 'string'), ('Heating', 'string'), ('Dwelling', 'string'),
            ('Income', 'real'), ('Weight', 'real')],
}
# Tableau saves the first conditional without a space after IF: "IF[Table].[Field]".
JOIN_TERMS = [
    ('[Housing].[Zone]', '[Households].[ZoneCode]'),
    ('[Housing].[Status]',
     'IF[Households].[Heating]="district" THEN "client" ELSE "prospect" END'),
    ('[Housing].[Kind]',
     'IF [Households].[Dwelling]="flat" THEN "collective" '
     'ELSEIF [Households].[Dwelling]="house" THEN "individual" ELSE "other" END'),
]
# (internal name, caption, role, datatype, formula)
CALCULATIONS = [
    # A row calculation declared as a measure; reading a parameter keeps it in DAX.
    ('Calculation_heat', 'Heating cost', 'measure', 'real', '[Usage] * [Parameters].[Parameter 1]'),
    # Calculated columns calling a measure, then each other: a circular
    # dependency once the first one stops being BLANK().
    ('Calculation_bill', 'Bill per home', 'dimension', 'real',
     '[Calculation_heat] / [Homes]'),
    ('Calculation_total', 'Total bill', 'dimension', 'real', '[Calculation_bill] + [Fee]'),
    # A row calculation reading both sides of the physical join.
    ('Calculation_poor', 'Poor weight', 'measure', 'real',
     'IF [Calculation_total] > [Parameters].[Parameter 1] * [Income] THEN [Weight] ELSE 0 END'),
    # FIXED ratios: a local row measure over an operand of the other table,
    # and an operand absent from every declaration.
    ('Calculation_rate', 'Poor rate', 'measure', 'real',
     'SUM({ FIXED [Sector] : SUM([Calculation_poor]) / SUM([Weight]) })'),
    ('Calculation_ghost', 'Ghost rate', 'measure', 'real',
     'SUM({ FIXED [Sector] : SUM([Usage]) / SUM([Ghost]) })'),
    # A ratio of two FIXED expressions: one of the two documented limits.
    ('Calculation_unsure', 'Unsure share', 'measure', 'real',
     '{ FIXED [Sector] : MIN([Usage]) } / { FIXED [Sector] : MIN([Homes]) }'),
    # An aggregate over a FIXED ratio measure.
    ('Calculation_scaled', 'Scaled share', 'measure', 'real',
     'MIN([Calculation_rate]) * MIN([Homes])'),
    # A text measure built from numeric aggregates.
    ('Calculation_level', 'Level', 'measure', 'string',
     'IF AVG([Homes]) > 2 * SUM([Weight]) THEN "high" ELSE "low" END'),
]
DOCUMENTED_LIMITS = {'Unsure share'}

_AGGREGATES = ('SUM', 'AVERAGE', 'MIN', 'MAX', 'COUNT', 'MEDIAN', 'PRODUCT')
_ROW_ITERATOR_RE = re.compile(
    r"\b(?:SUMX|AVERAGEX|MINX|MAXX|COUNTX|PRODUCTX)\s*\(\s*'((?:[^']|'')+)'\s*,", re.IGNORECASE)
_QUALIFIED_RE = re.compile(r"'((?:[^']|'')+)'\[((?:[^\]]|\]\])+)\]")
_COMMENT_RE = re.compile(r'/\*.*?\*/', re.DOTALL)


def _expression(parent, op):
    return ET.SubElement(parent, 'expression', op=op)


def _table(alias):
    relation = ET.Element('relation', type='table', name=alias,
                          connection=alias + '-ref', table=f'[{alias}$]')
    columns = ET.SubElement(relation, 'columns')
    for ordinal, (name, datatype) in enumerate(FIELDS[alias]):
        ET.SubElement(columns, 'column', name=name, datatype=datatype, ordinal=str(ordinal))
    return relation


def _source_xml():
    datasource = ET.Element('datasource', name=SOURCE, caption=SOURCE)
    connection = ET.SubElement(datasource, 'connection', {'class': 'federated'})
    for alias in FIELDS:
        named = ET.SubElement(connection, 'named-connection', name=alias + '-ref')
        ET.SubElement(named, 'connection', {
            'class': 'excel-direct', 'directory': 'Data', 'filename': alias + '.xlsx'})
    join = ET.SubElement(connection, 'relation', type='join', join='left')
    clause = ET.SubElement(join, 'clause', type='join')
    conjunction = _expression(clause, 'AND')
    for left, right in JOIN_TERMS:
        equality = _expression(conjunction, '=')
        _expression(equality, left)
        _expression(equality, right)
    join.extend((_table(LEFT), _table(RIGHT)))
    records = ET.SubElement(connection, 'metadata-records')
    maps = ET.SubElement(connection, 'cols')
    for alias, columns in FIELDS.items():
        for name, datatype in columns:
            record = ET.SubElement(records, 'metadata-record', {'class': 'column'})
            for tag, value in (('local-name', f'[{name}]'), ('remote-name', name),
                               ('parent-name', f'[{alias}]'), ('local-type', datatype)):
                ET.SubElement(record, tag).text = value
            ET.SubElement(maps, 'map', key=f'[{name}]', value=f'[{alias}].[{name}]')
            ET.SubElement(datasource, 'column', name=f'[{name}]', datatype=datatype)
    for name, caption, role, datatype, formula in CALCULATIONS:
        column = ET.SubElement(datasource, 'column', {
            'caption': caption, 'datatype': datatype, 'name': f'[{name}]', 'role': role,
            'type': 'quantitative' if datatype != 'string' else 'nominal'})
        ET.SubElement(column, 'calculation', {'class': 'tableau', 'formula': formula})
    return datasource


PARAMETER = {'name': '[Parameter 1]', 'caption': 'Unit price', 'datatype': 'real',
             'value': '0.5', 'domain_type': 'any', 'allowable_values': []}


def _parameters_xml():
    datasource = ET.Element('datasource', hasconnection='false', inline='true', name='Parameters')
    column = ET.SubElement(datasource, 'column', {
        'caption': PARAMETER['caption'], 'datatype': 'real', 'name': PARAMETER['name'],
        'param-domain-type': 'any', 'role': 'measure', 'type': 'quantitative',
        'value': PARAMETER['value']})
    ET.SubElement(column, 'calculation', {'class': 'tableau', 'formula': PARAMETER['value']})
    return datasource


def build_model():
    """The semantic model the batch migration builds for the fabricated workbook."""
    datasources = [extract_datasource(_parameters_xml()), extract_datasource(_source_xml())]
    return _build_semantic_model(
        datasources, report_name='Acceptance', model_mode='import',
        extra_objects={'_tolerate_unrepresentable_physical_joins': True,
                       'parameters': [dict(PARAMETER)]})


def _code(expression):
    return _COMMENT_RE.sub(' ', expression or '')


def _closing(text, opening):
    depth = 0
    for index in range(opening, len(text)):
        depth += {'(': 1, ')': -1}.get(text[index], 0)
        if depth == 0:
            return index
    return len(text)


class _Model:
    def __init__(self, model):
        self.tables = model['model']['tables']
        self.measures = {m['name']: m for t in self.tables for m in t.get('measures', [])}
        self.columns = {(t['name'], c['name']): c
                        for t in self.tables for c in t.get('columns', [])}
        self.home = {m['name']: t['name'] for t in self.tables for m in t.get('measures', [])}

    def measure_code(self, caption):
        return _code(self.measures[caption]['expression'])


class TestReportedWorkbookAcceptance(unittest.TestCase):
    """Each test names the Desktop failure it rules out."""

    @classmethod
    def setUpClass(cls):
        cls.model = _Model(build_model())

    def test_every_calculation_is_emitted_as_a_measure_or_column(self):
        emitted = set(self.model.measures) | {name for _, name in self.model.columns}
        missing = [caption for _, caption, *_ in CALCULATIONS if caption not in emitted]
        self.assertEqual(missing, [])

    def test_no_blank_measure_beyond_the_documented_limits(self):
        blank = sorted(name for name, measure in self.model.measures.items()
                       if 'TODO' in measure['expression']
                       or _code(measure['expression']).strip().endswith('BLANK()'))
        self.assertEqual(blank, sorted(DOCUMENTED_LIMITS))

    def test_no_blank_calculated_column(self):
        blank = sorted(name for (_, name), column in self.model.columns.items()
                       if column.get('expression') and 'TODO' in column['expression'])
        self.assertEqual(blank, [])

    def test_every_recovered_measure_carries_a_warning(self):
        recovered = ('Bill per home', 'Total bill', 'Poor weight', 'Poor rate',
                     'Ghost rate', 'Scaled share')
        unwarned = [name for name in recovered
                    if not self.model.measures.get(name, {}).get('_migration_note')]
        self.assertEqual(unwarned, [])

    def test_aggregated_placeholder_columns_are_numeric(self):
        """Rules out "SUM cannot work with values of type String"."""
        offending = []
        for (table, name), column in self.model.columns.items():
            if column.get('expression', '').strip() != 'BLANK()':
                continue
            reference = re.escape("'" + table.replace("'", "''") + "'[" + name + ']')
            aggregated = re.compile(rf"\b(?:{'|'.join(_AGGREGATES)})\s*\(\s*{reference}\s*\)",
                                    re.IGNORECASE)
            if (any(aggregated.search(_code(m['expression'])) for m in self.model.measures.values())
                    and column.get('dataType') not in ('double', 'int64', 'decimal')):
                offending.append(name)
        self.assertEqual(offending, [])

    def test_no_aggregate_over_a_measure(self):
        """Rules out "MIN only accepts a column reference"."""
        names = '|'.join(re.escape(name) for name in self.model.measures)
        pattern = re.compile(
            rf"\b(?:{'|'.join(_AGGREGATES)})\s*\(\s*(?:'[^']*')?\[(?:{names})\]\s*\)",
            re.IGNORECASE)
        offending = sorted(name for name, measure in self.model.measures.items()
                           if pattern.search(_code(measure['expression'])))
        self.assertEqual(offending, [])

    def test_row_iterators_read_other_tables_only_through_a_join(self):
        """Rules out "a single value for column ... cannot be determined"."""
        offending = []
        for name, measure in self.model.measures.items():
            code = _code(measure['expression'])
            for match in _ROW_ITERATOR_RE.finditer(code):
                iterated = match.group(1).replace("''", "'")
                body = code[match.end():_closing(code, code.index('(', match.start()))]
                body = re.sub(
                    r"\b(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTROWS|DISTINCTCOUNT|VALUES|FILTER|"
                    r"ALL|ALLEXCEPT|SELECTEDVALUE)\s*\([^()]*\)", ' ', body, flags=re.IGNORECASE)
                foreign = {owner.replace("''", "'") for owner, column in _QUALIFIED_RE.findall(body)
                           if owner.replace("''", "'") != iterated
                           and (owner.replace("''", "'"), column) in self.model.columns}
                if foreign:
                    offending.append((name, iterated, sorted(foreign)))
        self.assertEqual(offending, [])

    def test_the_row_join_carries_every_declared_join_condition(self):
        code = self.model.measure_code('Poor weight')
        self.assertRegex(code, r'\bGENERATEALL\s*\(')
        joined = code[code.index('GENERATEALL'):]
        for column in ('ZoneCode', 'Heating', 'Dwelling'):
            self.assertIn(f"'{RIGHT}'[{column}]", joined)
        for column in ('Zone', 'Status', 'Kind'):
            self.assertIn(f"'{LEFT}'[{column}]", joined)

    def test_ratio_operands_of_another_table_and_absent_ones_are_kept(self):
        self.assertIn(f"'{RIGHT}'[Weight]", self.model.measure_code('Poor rate'))
        self.assertIn(f"'{LEFT}'[Ghost]", self.model.measure_code('Ghost rate'))

    def test_no_numeric_companion_wraps_a_text_measure(self):
        """Rules out "cannot convert value of type Text"."""
        self.assertNotIn('Level Formatted', self.model.measures)

    def test_no_calculated_column_calls_a_measure(self):
        """Rules out the circular dependency that stops the project opening."""
        offending = []
        for (table, name), column in self.model.columns.items():
            code = _code(column.get('expression'))
            for owner, reference in re.findall(r"(?:'((?:[^']|'')+)')?\[((?:[^\]]|\]\])+)\]", code):
                owner = owner.replace("''", "'") or table
                if reference in self.model.measures and (owner, reference) not in self.model.columns:
                    offending.append((name, reference))
        self.assertEqual(offending, [])


if __name__ == '__main__':
    unittest.main()