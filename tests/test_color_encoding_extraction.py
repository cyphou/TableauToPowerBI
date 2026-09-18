"""Colour encodings must be read from the shape real workbooks use.

Tableau splits a colour encoding in two: the worksheet names the field and the
palette, while the per-value colours live once per datasource. Reading only the
worksheet half silently dropped every colour the workbook defined — measured
across the example corpus as 72 colour-encoded worksheets producing zero
coloured visuals.

The XML in these tests mirrors genuine workbooks: ``<color>`` carries no
palette attribute, ``<bucket>`` carries its value as text rather than as
attributes, and ``<color-palette>`` is declared at document level.
"""

import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from tableau_export.extract_tableau_data import (  # noqa: E402
    TableauExtractor,
    _strip_datasource_prefix,
)

#: A workbook as Tableau actually writes one.
_WORKBOOK = '''<workbook>
  <preferences>
    <color-palette name="Open-win-Lost" type="regular" custom="true">
      <color>#c85200</color>
      <color>#1170aa</color>
    </color-palette>
  </preferences>
  <datasources>
    <datasource name="federated.abc">
      <style>
        <style-rule element="mark">
          <encoding attr="color" field="[none:Sub-Category:nk]" type="palette">
            <map to="#499894"><bucket>&quot;Accessories&quot;</bucket></map>
            <map to="#4e79a7"><bucket>&quot;Labels&quot;</bucket></map>
            <map to="#59a14f"><bucket>%null%</bucket></map>
          </encoding>
        </style-rule>
      </style>
    </datasource>
  </datasources>
  <worksheet name="Sales by Sub-Category">
    <table>
      <view><pane><mark class="Bar"/></pane></view>
      <style>
        <style-rule element="mark">
          <encoding attr="color" palette="Open-win-Lost" type="palette"/>
        </style-rule>
      </style>
    </table>
    <encodings>
      <color column="[federated.abc].[none:Sub-Category:nk]"/>
    </encodings>
  </worksheet>
</workbook>'''


def _extract(xml=_WORKBOOK):
    extractor = TableauExtractor.__new__(TableauExtractor)
    extractor._xml_node_cache = {}
    root = ET.fromstring(xml)
    extractor._color_index = extractor._build_color_index(root)
    worksheet = root.find('.//worksheet')
    return extractor.extract_mark_encoding(worksheet)['color']


class TestDatasourcePrefixMatching(unittest.TestCase):
    def test_prefix_is_stripped(self):
        self.assertEqual(
            _strip_datasource_prefix('[federated.abc].[none:Region:nk]'),
            '[none:Region:nk]')

    def test_bare_field_is_unchanged(self):
        self.assertEqual(_strip_datasource_prefix('[none:Region:nk]'),
                         '[none:Region:nk]')

    def test_empty_is_safe(self):
        self.assertEqual(_strip_datasource_prefix(''), '')
        self.assertEqual(_strip_datasource_prefix(None), '')


class TestColorIndex(unittest.TestCase):
    def test_per_value_colours_come_from_the_datasource(self):
        index = TableauExtractor._build_color_index(
            TableauExtractor.__new__(TableauExtractor), ET.fromstring(_WORKBOOK))
        self.assertEqual(index['maps']['[none:Sub-Category:nk]'],
                         {'Accessories': '#499894', 'Labels': '#4e79a7'})

    def test_null_bucket_is_not_a_value(self):
        index = TableauExtractor._build_color_index(
            TableauExtractor.__new__(TableauExtractor), ET.fromstring(_WORKBOOK))
        self.assertNotIn('%null%', index['maps']['[none:Sub-Category:nk]'])

    def test_document_level_palettes_are_indexed(self):
        index = TableauExtractor._build_color_index(
            TableauExtractor.__new__(TableauExtractor), ET.fromstring(_WORKBOOK))
        self.assertEqual(index['palettes']['Open-win-Lost'],
                         ['#c85200', '#1170aa'])


class TestMarkEncodingJoinsBothHalves(unittest.TestCase):
    def test_worksheet_colour_field_gains_the_datasource_colours(self):
        color = _extract()
        self.assertEqual(color['color_values'],
                         {'Accessories': '#499894', 'Labels': '#4e79a7'})

    def test_palette_name_comes_from_the_worksheet_style_rule(self):
        """`<color>` never carries a palette attribute in real workbooks."""
        color = _extract()
        self.assertEqual(color['palette'], 'Open-win-Lost')

    def test_named_palette_resolves_to_its_colours(self):
        color = _extract()
        self.assertEqual(color['palette_colors'], ['#c85200', '#1170aa'])

    def test_ramp_kind_is_translated_to_the_shared_vocabulary(self):
        """Tableau says 'palette'/'interpolated'; the pipeline says
        'categorical'/'quantitative'."""
        color = _extract()
        self.assertEqual(color['type'], 'categorical')

    def test_interpolated_ramp_reads_as_quantitative(self):
        xml = _WORKBOOK.replace(
            '<encoding attr="color" palette="Open-win-Lost" type="palette"/>',
            '<encoding attr="color" palette="Open-win-Lost" type="interpolated"/>'
        ).replace('[none:Sub-Category:nk]"/>', '[avg:Profit:qk]"/>')
        color = _extract(xml)
        self.assertEqual(color['type'], 'quantitative')

    def test_unmatched_field_gets_no_colour_values(self):
        xml = _WORKBOOK.replace(
            '<color column="[federated.abc].[none:Sub-Category:nk]"/>',
            '<color column="[federated.abc].[none:Segment:nk]"/>')
        color = _extract(xml)
        self.assertNotIn('color_values', color)

    def test_extraction_works_without_an_index(self):
        """extract_mark_encoding is called directly by other tests."""
        extractor = TableauExtractor.__new__(TableauExtractor)
        extractor._xml_node_cache = {}
        worksheet = ET.fromstring(_WORKBOOK).find('.//worksheet')
        color = extractor.extract_mark_encoding(worksheet)['color']
        self.assertEqual(color['field'], 'Sub-Category')
        self.assertNotIn('color_values', color)


if __name__ == '__main__':
    unittest.main()
