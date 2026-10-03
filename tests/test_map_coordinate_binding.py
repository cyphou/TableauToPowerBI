"""Power BI refuses to plot a Location beside raw, unaggregated coordinates.

Observed in Power BI Desktop: an azureMap bound to a Location plus bare
latitude/longitude columns draws nothing and reports "Remove Location to
display latitude and longitude pairs. Alternatively, you can also keep Location
and set the aggregate for Latitude and Longitude to Average." The capability
catalog makes Category (the Location well) required for azureMap, so the only
workable combination is Location + averaged coordinates, and that is what finally
rendered bubbles on the Azure Maps base map.

Two things have to hold: the model must not summarise a coordinate column, and
the visual must bind latitude (Y) and longitude (X) with Average (Function 1) --
never Sum (Function 0), which is what left the map blank.
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
from powerbi_import.visual_generator import resolve_visual_type

# The Power BI capability catalog declares requiredRoles ['Category'] for
# azureMap, so a map that resolves no location is rebuilt as a table.
NO_LOCATION_NOTE = ('azureMap needs Category; source fields could not fill '
                    'that role, shown as a table instead')
# The specific cause recorded by the azureMap branch before the generic
# contract degrades the visual. Both must survive in the note.
NO_GEOGRAPHY_NOTE = ('No resolved geographic location or coordinate pair; '
                     'source geography is required for this map.')


def assert_unresolved_geography_note(case, note):
    """The specific geography cause must come first and the generic contract
    cause must follow it; a reader needs to know why the map lost its map."""
    case.assertIn(NO_GEOGRAPHY_NOTE, note)
    case.assertIn(NO_LOCATION_NOTE, note)
    case.assertLess(note.index(NO_GEOGRAPHY_NOTE), note.index(NO_LOCATION_NOTE))
    case.assertEqual(note, '%s; %s' % (NO_GEOGRAPHY_NOTE, NO_LOCATION_NOTE))


def _bound_properties(role_body):
    """Field names bound to a role, whatever wrapper carries them."""
    names = []
    for projection in role_body.get('projections', []):
        field = projection['field']
        holder = field.get('Aggregation', {}).get('Expression', field)
        for wrapper in ('Column', 'Measure'):
            if wrapper in holder:
                names.append(holder[wrapper]['Property'])
    return names


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
        # 'City' fills the Category role the catalog requires, so the
        # coordinate wells below are asserted on a map that stays a map.
        return [
            {'name': 'Latitude', 'shelf': 'rows', 'semantic_role': '[Latitude]'},
            {'name': 'Longitude', 'shelf': 'columns',
             'semantic_role': '[Longitude]'},
            {'name': 'City', 'shelf': 'detail'},
            {'name': 'Amount', 'shelf': 'size'},
        ]

    def _roles(self, query):
        return query.get('queryState', {})

    def test_coordinates_reach_their_own_wells(self):
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        ws = self._ws(self._coords())
        roles = self._roles(gen._build_visual_query(ws))
        self.assertNotIn('_override_visual_type', ws)
        for role, name in (('Y', 'Latitude'), ('X', 'Longitude')):
            projections = roles[role]['projections']
            self.assertEqual(len(projections), 1, role)
            self.assertEqual(projections[0]['field'], {
                'Aggregation': {
                    'Expression': {'Column': {
                        'Expression': {'SourceRef': {'Entity': 'Sites'}},
                        'Property': name}},
                    'Function': 1,
                },
            }, role)
        self.assertEqual(
            roles['Category']['projections'][0]['field']['Column']['Property'],
            'City')

    def test_coordinates_are_averaged_not_summed(self):
        # Beside a bound Location, Desktop accepts Average (1) only; the Sum (0)
        # the numeric default would pick leaves the map blank.
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(self._ws(self._coords())))
        for role, name in (('Y', 'Latitude'), ('X', 'Longitude')):
            field = roles[role]['projections'][0]['field']
            self.assertIn('Aggregation', field, role)
            self.assertNotIn('Column', field, role)
            self.assertEqual(
                field['Aggregation']['Expression']['Column']['Property'],
                name, role)
            self.assertEqual(field['Aggregation']['Function'], 1, role)
            self.assertNotEqual(field['Aggregation']['Function'], 0, role)

    def test_coordinates_without_location_keep_the_bare_column_path(self):
        # No dimension can fill Location, so the coordinates stay bare columns
        # and the required-role contract degrades the map to a table.
        ws = self._ws([
            {'name': 'Latitude', 'shelf': 'rows', 'semantic_role': '[Latitude]'},
            {'name': 'Longitude', 'shelf': 'columns',
             'semantic_role': '[Longitude]'},
            {'name': 'Amount', 'shelf': 'size'},
        ])
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(ws))
        self.assertEqual(ws['_override_visual_type'], 'tableEx')
        self.assertIn(NO_LOCATION_NOTE, ws['_visual_mapping_note'])
        self.assertEqual(set(roles), {'Values'})
        self.assertEqual(set(_bound_properties(roles['Values'])),
                         {'Latitude', 'Longitude', 'Amount'})

    def test_measure_still_reaches_size(self):
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(self._ws(self._coords())))
        self.assertEqual(_bound_properties(roles['Size']), ['Amount'])

    def test_coordinates_are_not_repeated_in_other_wells(self):
        gen = self._generator(measures={'Latitude', 'Longitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(self._ws(self._coords())))
        for role, body in roles.items():
            if role in ('Y', 'X'):
                continue
            for p in body.get('projections', []):
                self.assertNotIn(p.get('queryRef'),
                                 ('Sites.Latitude', 'Sites.Longitude'))

    def test_missing_coordinate_and_location_records_unresolved_geography(self):
        # A lone latitude cannot plot and 'Product Label' is not geographic,
        # so the map degrades to a table that still carries every pill.
        fields = [
            {'name': 'Latitude', 'shelf': 'rows', 'semantic_role': '[Latitude]'},
            {'name': 'Product Label', 'shelf': 'detail'},
            {'name': 'Amount', 'shelf': 'size'},
        ]
        ws = self._ws(fields)
        gen = self._generator(measures={'Latitude', 'Amount'})
        roles = self._roles(gen._build_visual_query(ws))
        self.assertEqual(ws['_override_visual_type'], 'tableEx')
        assert_unresolved_geography_note(self, ws['_visual_mapping_note'])
        self.assertEqual(set(roles), {'Values'})
        self.assertEqual(_bound_properties(roles['Values']),
                         ['Product Label', 'Latitude', 'Amount'])

    def test_location_detail_keeps_azure_maps_without_coordinates(self):
        ws = self._ws([
            {'name': 'City', 'shelf': 'detail'},
            {'name': 'Amount', 'shelf': 'size'},
        ])
        roles = self._roles(self._generator(measures={'Amount'})
                            ._build_visual_query(ws))
        self.assertIn('Category', roles)
        self.assertEqual(roles['Category']['projections'][0]['field']['Column']['Property'],
                 'City')
        self.assertNotIn('_override_visual_type', ws)

    def test_location_tooltip_keeps_azure_maps_without_coordinates(self):
        ws = self._ws([
            {'name': 'Country', 'shelf': 'tooltip'},
            {'name': 'Amount', 'shelf': 'size'},
        ])
        roles = self._roles(self._generator(measures={'Amount'})
                            ._build_visual_query(ws))
        self.assertIn('Category', roles)
        self.assertEqual(roles['Category']['projections'][0]['field']['Column']['Property'],
                 'Country')
        self.assertNotIn('_override_visual_type', ws)

    def test_generated_coordinates_use_named_region_text_as_location(self):
        ws = self._ws([
            {'name': 'Latitude (generated)', 'shelf': 'rows'},
            {'name': 'Longitude (generated)', 'shelf': 'columns'},
            {'name': 'Nom Région', 'shelf': 'text'},
            {'name': 'Amount', 'shelf': 'size'},
        ])
        roles = self._roles(self._generator(measures={'Amount'})
                            ._build_visual_query(ws))
        self.assertIn('Category', roles)
        self.assertEqual(roles['Category']['projections'][0]['field']['Column']['Property'],
                 'Nom Région')
        self.assertNotIn('_override_visual_type', ws)

    def test_generated_geography_does_not_geocode_non_geographic_text(self):
        ws = self._ws([
            {'name': 'Latitude (generated)', 'shelf': 'rows'},
            {'name': 'Longitude (generated)', 'shelf': 'columns'},
            {'name': 'Plant Name', 'shelf': 'text'},
            {'name': 'Revenue', 'shelf': 'tooltip'},
            {'name': 'Profit', 'shelf': 'tooltip'},
        ])
        gen = self._generator(measures={'Revenue', 'Profit'})
        roles = self._roles(gen._build_visual_query(ws))
        self.assertEqual(ws['_override_visual_type'], 'tableEx')
        assert_unresolved_geography_note(self, ws['_visual_mapping_note'])
        # 'Plant Name' survives as a plain table column, never as a Location.
        self.assertEqual(set(roles), {'Values'})
        self.assertEqual(_bound_properties(roles['Values']),
                         ['Plant Name', 'Revenue', 'Profit'])


class TestAzureMapsDefaults(unittest.TestCase):

    def test_standard_tableau_geographies_default_to_azure_maps(self):
        for chart_type in ('map', 'geomap', 'density'):
            self.assertEqual(resolve_visual_type(chart_type), 'azureMap')


class TestModelCategoryDrivesLocation(unittest.TestCase):
    """Tableau may declare geography only on the datasource column, so the
    worksheet pill carries no semantic_role. The generated model still holds
    the dataCategory, and that must be what fills Azure Maps' Location well.

    'Zone Label' is deliberately free of geographic words, so any Category
    binding here can only come from the model's dataCategory.
    """

    TABLE = 'Sites'
    FIELD = 'Zone Label'

    def _generator(self, categories):
        gen = PowerBIProjectGenerator.__new__(PowerBIProjectGenerator)
        gen._field_map = {}
        gen._main_table = self.TABLE
        gen._measure_names = {'Amount'}
        gen._bim_measure_names = {'Amount'}
        gen._actual_bim_measure_names = {'Amount'}
        gen._actual_bim_symbols = {(self.TABLE, self.FIELD),
                                   (self.TABLE, 'Amount')}
        gen._actual_bim_column_categories = dict(categories)
        gen._datasources_ref = []
        gen._collision_tables = set()
        gen._unavailable_parameter_names = set()
        return gen

    def _ws(self):
        return {'name': 'Map', 'chart_type': 'azureMap', 'fields': [
            {'name': self.FIELD, 'shelf': 'text'},
            {'name': 'Amount', 'shelf': 'size'},
        ]}

    def _run(self, categories):
        ws = self._ws()
        query = self._generator(categories)._build_visual_query(ws)
        return ws, query.get('queryState', {})

    def _all_properties(self, role_body):
        return [p['field'][wrapper]['Property']
                for p in role_body.get('projections', [])
                for wrapper in ('Column', 'Measure')
                if wrapper in p['field']]

    def test_model_category_fills_location(self):
        ws, roles = self._run({(self.TABLE, self.FIELD): 'county'})
        self.assertIn('Category', roles)
        self.assertEqual(roles['Category']['projections'], [{
            'field': {'Column': {
                'Expression': {'SourceRef': {'Entity': self.TABLE}},
                'Property': self.FIELD,
            }},
            'queryRef': '%s.%s' % (self.TABLE, self.FIELD),
            'nativeQueryRef': self.FIELD,
            'active': True,
        }])
        self.assertNotIn('_visual_mapping_note', ws)

    def test_location_is_not_duplicated_in_tooltips(self):
        # The same pill reaching both Location and Tooltips makes Azure Maps
        # render the label twice for every point.
        _, roles = self._run({(self.TABLE, self.FIELD): 'county'})
        self.assertNotIn(self.FIELD,
                         self._all_properties(roles.get('Tooltips', {})))

    def test_measure_still_reaches_size(self):
        _, roles = self._run({(self.TABLE, self.FIELD): 'county'})
        self.assertEqual(self._all_properties(roles.get('Size', {})), ['Amount'])

    def _assert_degraded_to_table(self, ws, roles):
        """Nothing reached Location, so the map became a table that still
        carries both source pills."""
        self.assertEqual(ws['_override_visual_type'], 'tableEx')
        assert_unresolved_geography_note(self, ws['_visual_mapping_note'])
        self.assertEqual(set(roles), {'Values'})
        self.assertEqual(_bound_properties(roles['Values']),
                         [self.FIELD, 'Amount'])

    def test_without_model_category_location_stays_unresolved(self):
        # Negative control: no dataCategory and no geographic word in the
        # name, so nothing may be promoted to Location.
        self._assert_degraded_to_table(*self._run({}))

    def test_category_lookup_is_keyed_by_table_and_column(self):
        # A geography on a different column must not leak onto this field.
        self._assert_degraded_to_table(
            *self._run({(self.TABLE, 'Other Column'): 'county'}))

    def test_non_geographic_category_is_not_a_location(self):
        self._assert_degraded_to_table(
            *self._run({(self.TABLE, self.FIELD): 'barcode'}))

    def test_every_geographic_category_is_accepted(self):
        for category in ('city', 'country', 'county', 'stateorprovince',
                         'postalcode', 'continent'):
            with self.subTest(category=category):
                ws, roles = self._run({(self.TABLE, self.FIELD): category})
                self.assertIn('Category', roles, category)
                self.assertEqual(
                    roles['Category']['projections'][0]['field']['Column']['Property'],
                    self.FIELD)
                self.assertNotIn('_visual_mapping_note', ws)


if __name__ == '__main__':
    unittest.main()
