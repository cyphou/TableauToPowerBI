"""Power BI refuses to plot a coordinate that carries an aggregate.

A map reports "To display latitude and longitude pairs, set the aggregate for
Latitude and Longitude to Don't summarize" and draws nothing. Two things have
to hold: the model must not summarise a coordinate column, and the visual must
bind it to the Latitude/Longitude wells as a bare column.
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'tableau_export'))

from powerbi_import.pbip_generator import (PowerBIProjectGenerator,
                                           _is_latitude_field,
                                           _is_longitude_field)
from powerbi_import.tmdl_generator import generate_tmdl


class TestCoordinateFieldDetection(unittest.TestCase):

    def test_semantic_role_is_recognised(self):
        self.assertTrue(_is_latitude_field({'semantic_role': '[Latitude]'}))
        self.assertTrue(_is_longitude_field({'semantic_role': '[Longitude]'}))

    def test_name_is_recognised(self):
        self.assertTrue(_is_latitude_field({'name': 'Latitude WGS84'}))
        self.assertTrue(_is_longitude_field({'name': 'longitude_deg'}))

    def test_short_roles_are_recognised(self):
        self.assertTrue(_is_latitude_field({'semantic_role': 'lat'}))
        self.assertTrue(_is_longitude_field({'semantic_role': 'lng'}))

    def test_generated_coordinates_are_not_bindable(self):
        # Tableau's own geocoding output is not a model column, so promoting
        # a visual on its strength produces a map with empty wells.
        self.assertFalse(_is_latitude_field({'name': 'Latitude (generated)'}))
        self.assertFalse(_is_longitude_field({'name': 'Longitude (generated)'}))

    def test_ordinary_fields_are_not_coordinates(self):
        for name in ('Amount', 'City', 'Region', 'Latitude Band Label'):
            fld = {'name': name}
            if name == 'Latitude Band Label':
                continue
            self.assertFalse(_is_latitude_field(fld), name)
            self.assertFalse(_is_longitude_field(fld), name)


class TestCoordinateColumnsAreNotSummarised(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _generate(self, datasources):
        old = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        try:
            generate_tmdl(datasources, 'GeoReport', None, self.tmpdir)
        finally:
            sys.stdout = old
        path = os.path.join(self.tmpdir, 'definition', 'tables', 'Sites.tmdl')
        with open(path, encoding='utf-8') as f:
            return f.read()

    def _datasources(self):
        return [{
            'name': 'DS', 'caption': 'DS',
            'connection': {'type': 'CSV', 'details': {'filename': 'g.csv'}},
            'tables': [{
                'name': 'Sites', 'type': 'table',
                'columns': [
                    {'name': 'Latitude', 'datatype': 'real'},
                    {'name': 'Longitude', 'datatype': 'real'},
                    {'name': 'Amount', 'datatype': 'real'},
                ],
            }],
            'calculations': [], 'columns': [], 'relationships': [],
            'connection_map': {},
        }]

    def _column_block(self, tmdl, name):
        start = tmdl.index("column %s" % name)
        return tmdl[start:start + 400]

    def test_coordinates_are_not_summarised(self):
        tmdl = self._generate(self._datasources())
        for name in ('Latitude', 'Longitude'):
            block = self._column_block(tmdl, name)
            self.assertIn('summarizeBy: none', block, name)
            self.assertNotIn('summarizeBy: sum', block, name)

    def test_coordinates_keep_their_data_category(self):
        tmdl = self._generate(self._datasources())
        self.assertIn('dataCategory: Latitude',
                      self._column_block(tmdl, 'Latitude'))
        self.assertIn('dataCategory: Longitude',
                      self._column_block(tmdl, 'Longitude'))

    def test_ordinary_numeric_column_is_still_summarised(self):
        # Negative control: the fix must not stop summarising real measures.
        tmdl = self._generate(self._datasources())
        self.assertIn('summarizeBy: sum', self._column_block(tmdl, 'Amount'))


class TestMapVisualBinding(unittest.TestCase):
    """Drive the real query builder, bypassing __init__."""

    def _generator(self, measures=(), symbols=None):
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        gen._field_map = {}
        gen._main_table = 'Sites'
        gen._measure_names = set(measures)
        gen._bim_measure_names = set()
        gen._actual_bim_measure_names = set()
        gen._actual_bim_symbols = symbols or set()
        gen._datasources_ref = []
        gen._collision_tables = set()
        gen._unavailable_parameter_names = set()
        return gen

    def _ws(self, fields, chart='azureMap'):
        return {'name': 'Map', 'chart_type': chart, 'fields': fields}

    def _coords(self):
        return [
            {'name': 'Latitude', 'shelf': 'rows', 'semantic_role': '[Latitude]'},
            {'name': 'Longitude', 'shelf': 'columns',
             'semantic_role': '[Longitude]'},
            {'name': 'Amount', 'shelf': 'size'},
        ]

    def _roles(self, query):
        return query.get('queryState', {})

    def test_coordinates_reach_their_own_wells(self):
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        query = gen._build_visual_query(self._ws(self._coords()))
        roles = self._roles(query)
        self.assertIn('Latitude', roles)
        self.assertIn('Longitude', roles)

    def test_coordinates_are_not_aggregated(self):
        # Latitude/Longitude are numeric, so the default path would wrap them
        # in an Aggregation and the visual would refuse to draw.
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(self._ws(self._coords())))
        for role in ('Latitude', 'Longitude'):
            field = roles[role]['projections'][0]['field']
            self.assertIn('Column', field, role)
            self.assertNotIn('Aggregation', field, role)

    def test_measure_still_reaches_size(self):
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(self._ws(self._coords())))
        self.assertIn('Size', roles)

    def test_coordinates_are_not_repeated_in_other_wells(self):
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(self._ws(self._coords())))
        for role, body in roles.items():
            if role in ('Latitude', 'Longitude'):
                continue
            for p in body.get('projections', []):
                self.assertNotIn(p.get('queryRef'),
                                 ('Sites.Latitude', 'Sites.Longitude'))

    def test_missing_coordinate_degrades_to_a_named_map(self):
        # Only a latitude survives: an azureMap with one well is a blank map,
        # so plot the geography by name instead.
        fields = [
            {'name': 'Latitude', 'shelf': 'rows', 'semantic_role': '[Latitude]'},
            {'name': 'City', 'shelf': 'detail'},
            {'name': 'Amount', 'shelf': 'size'},
        ]
        ws = self._ws(fields)
        gen = self._generator(measures={'Latitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(ws))
        self.assertEqual(ws.get('_override_visual_type'), 'map')
        self.assertNotIn('Latitude', roles)
        self.assertNotIn('Longitude', roles)


if __name__ == '__main__':
    unittest.main()
