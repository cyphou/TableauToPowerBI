"""A place name is text, so do not guess one from a numeric column's name.

_map_semantic_role_to_category falls back to the column name when Tableau
declares no semantic role. That fallback ignored the column's type, so an
integer key named RegionId became StateOrProvince and a postal code stored
as a double became PostalCode -- Power BI then treats the number as a
location and offers to map it.

Measured across both corpora before the fix: 13 numeric columns carried a
place category. After: 3, and all three come from a semantic role the
Tableau author declared explicitly, which is deliberately left alone. Only
the guess is corrected, never the declaration.

A rule considered and rejected on evidence: skipping names that end in a
key-like suffix. It flags `postal_code` and `Postal Code`, which really are
postal codes.
"""
import unittest

from powerbi_import.tmdl_generator import _map_semantic_role_to_category as cat


class TestAPlaceGuessNeedsATextColumn(unittest.TestCase):

    def test_an_integer_key_is_not_a_province(self):
        self.assertIsNone(cat('', 'RegionId', 'integer'))

    def test_a_postal_code_stored_as_a_number_is_left_alone(self):
        self.assertIsNone(cat('', 'postal_code', 'real'))

    def test_a_numeric_commune_code_is_not_a_city(self):
        self.assertIsNone(cat('', 'commune', 'integer'))

    def test_a_boolean_is_not_a_country(self):
        self.assertIsNone(cat('', 'pays', 'boolean'))

    def test_a_date_is_not_a_place(self):
        self.assertIsNone(cat('', 'region', 'datetime'))


class TestTextColumnsStillGetTheirCategory(unittest.TestCase):

    def test_a_text_region_is_still_a_province(self):
        self.assertEqual(cat('', 'region', 'string'), 'StateOrProvince')

    def test_a_text_city_is_still_a_city(self):
        self.assertEqual(cat('', 'ville', 'string'), 'City')

    def test_a_text_postal_code_is_still_a_postal_code(self):
        self.assertEqual(cat('', 'code_postal', 'string'), 'PostalCode')

    def test_an_unknown_type_keeps_the_old_behaviour(self):
        # Callers that cannot say what the type is must not lose the guess.
        self.assertEqual(cat('', 'region'), 'StateOrProvince')
        self.assertEqual(cat('', 'region', None), 'StateOrProvince')


class TestCoordinatesAreNumericByNature(unittest.TestCase):
    """The type guard must not reach latitude and longitude."""

    def test_a_numeric_latitude_is_still_latitude(self):
        self.assertEqual(cat('', 'latitude', 'real'), 'Latitude')

    def test_a_numeric_longitude_is_still_longitude(self):
        self.assertEqual(cat('', 'longitude', 'real'), 'Longitude')

    def test_short_coordinate_names_survive(self):
        self.assertEqual(cat('', 'lat', 'real'), 'Latitude')
        self.assertEqual(cat('', 'lng', 'real'), 'Longitude')


class TestADeclaredRoleIsNotOverruled(unittest.TestCase):
    """Tableau's own semantic role is the author speaking, not a guess."""

    def test_a_declared_city_survives_a_numeric_column(self):
        self.assertEqual(cat('[City].[Name]', 'insee', 'integer'), 'City')

    def test_a_declared_zip_survives_a_numeric_column(self):
        self.assertEqual(cat('[ZipCode].[Name]', 'cp', 'real'), 'PostalCode')

    def test_a_declared_state_survives(self):
        self.assertEqual(cat('[State].[Name]', 'x', 'integer'),
                         'StateOrProvince')


class TestNothingIsInvented(unittest.TestCase):

    def test_an_unrelated_name_gets_no_category(self):
        self.assertIsNone(cat('', 'amount', 'string'))

    def test_an_unknown_role_falls_through_to_nothing(self):
        self.assertIsNone(cat('[Something].[Else]', 'amount', 'string'))


if __name__ == '__main__':
    unittest.main()
