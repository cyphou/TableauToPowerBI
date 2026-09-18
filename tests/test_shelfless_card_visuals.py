"""A Tableau sheet drawn only on the Marks card is a KPI card, not a table.

Tableau's standard "big number" sheet leaves Rows and Columns empty and puts
the measure on the Text shelf. Both the ``Text`` mark and the ``Automatic``
mark fell through to a data grid, which is what the example corpus showed:
28 of 181 worksheets became a table, 16 of them named "KPI Card", "Ranking"
or "Card".

The XML here mirrors real workbooks — empty ``<cols>``/``<rows>`` elements and
``<encodings>`` carrying the marks card.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from powerbi_import.visual_generator import resolve_visual_type  # noqa: E402
from tableau_export.extract_tableau_data import TableauExtractor  # noqa: E402


def _worksheet(mark='Text', cols='', rows='', encodings=''):
    return ET.fromstring(f'''<worksheet name="Sheet">
  <table>
    <view/>
    <cols>{cols}</cols>
    <rows>{rows}</rows>
    <panes>
      <pane>
        <mark class="{mark}"/>
        <encodings>{encodings}</encodings>
      </pane>
    </panes>
  </table>
</worksheet>''')


def _text(*names):
    return ''.join(f'<text column="[ds].[sum:{n}:qk]"/>' for n in names)


def _chart_type(ws):
    return TableauExtractor.determine_chart_type(
        TableauExtractor.__new__(TableauExtractor), ws)


class TestShelflessCard(unittest.TestCase):

    def test_one_text_field_and_no_shelves_is_a_card(self):
        self.assertEqual('card', _chart_type(
            _worksheet(mark='Text', encodings=_text('Sales'))))

    def test_several_text_fields_and_no_shelves_is_a_multi_row_card(self):
        self.assertEqual('multiRowCard', _chart_type(
            _worksheet(mark='Text', encodings=_text('Sales', 'Profit', 'Qty'))))

    def test_an_automatic_mark_takes_the_same_route(self):
        self.assertEqual('card', _chart_type(
            _worksheet(mark='Automatic', encodings=_text('Sales'))))

    def test_detail_and_tooltip_fields_do_not_make_it_a_table(self):
        encodings = (_text('Sales')
                     + '<detail column="[ds].[none:Region:nk]"/>'
                     + '<tooltip column="[ds].[none:Segment:nk]"/>')
        self.assertEqual('card', _chart_type(_worksheet(encodings=encodings)))

    def test_the_card_type_survives_visual_type_resolution(self):
        for source in ('card', 'multiRowCard'):
            with self.subTest(source=source):
                self.assertEqual(source, resolve_visual_type(source))


class TestItIsStillATableWhenItShouldBe(unittest.TestCase):
    """Without these the rule would turn genuine text tables into cards."""

    def test_a_field_on_columns_keeps_the_table(self):
        ws = _worksheet(mark='Text', cols='[ds].[none:Region:nk]',
                        encodings=_text('Sales'))
        self.assertEqual('tableEx', _chart_type(ws))

    def test_a_field_on_rows_keeps_the_table(self):
        ws = _worksheet(mark='Text', rows='[ds].[none:Region:nk]',
                        encodings=_text('Sales'))
        self.assertEqual('tableEx', _chart_type(ws))

    def test_no_text_encoding_is_not_a_card(self):
        ws = _worksheet(mark='Text',
                        encodings='<detail column="[ds].[none:Region:nk]"/>')
        self.assertEqual('tableEx', _chart_type(ws))

    def test_a_size_encoding_is_a_packed_bubble_not_a_card(self):
        encodings = (_text('Sales')
                     + '<size column="[ds].[sum:Sales:qk]"/>'
                     + '<color column="[ds].[none:Region:nk]"/>')
        self.assertNotIn(_chart_type(_worksheet(encodings=encodings)),
                         ('card', 'multiRowCard'))

    def test_a_bar_mark_is_never_a_card(self):
        self.assertEqual('clusteredBarChart', _chart_type(
            _worksheet(mark='Bar', encodings=_text('Sales'))))

    def test_a_line_mark_is_never_a_card(self):
        self.assertEqual('lineChart', _chart_type(
            _worksheet(mark='Line', encodings=_text('Sales'))))

    def test_the_non_standard_shelf_form_still_counts_as_a_shelf(self):
        ws = ET.fromstring('''<worksheet name="Sheet">
  <shelf-columns><field>[ds].[none:Region:nk]</field></shelf-columns>
  <table>
    <cols></cols><rows></rows>
    <panes><pane><mark class="Text"/>
      <encodings><text column="[ds].[sum:Sales:qk]"/></encodings>
    </pane></panes>
  </table>
</worksheet>''')
        self.assertEqual('tableEx', _chart_type(ws))

    def test_an_empty_worksheet_is_not_promoted_to_a_card(self):
        self.assertEqual('table', _chart_type(_worksheet(mark='Automatic')))


if __name__ == '__main__':
    unittest.main()
