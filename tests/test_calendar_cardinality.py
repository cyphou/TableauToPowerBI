"""Calendar is ours, so its key is unique as a matter of fact.

_detect_many_to_many guesses at uniqueness from column counts: when the
target table has at least 70% as many columns as the source, it calls them
peer tables and makes the relationship many-to-many. The generated Calendar
carries ~14 columns, so against any normal fact table it always looked like
a peer -- and that branch sat above the Calendar branch, which was therefore
never reached.

Measured on both corpora before the fix: 15 of 15 Calendar relationships in
the samples and 12 of 12 in the real workbooks were many-to-many. Every one.
"""
import unittest

from powerbi_import.tmdl_relationships import _detect_many_to_many


def _model(relationships, calendar_columns=14, fact_columns=4):
    return {'model': {
        'tables': [
            {'name': 'Calendar',
             'columns': [{'name': 'c%d' % i} for i in range(calendar_columns)]},
            {'name': 'Orders',
             'columns': [{'name': 'f%d' % i} for i in range(fact_columns)]},
            {'name': 'Regions', 'columns': [{'name': 'RegionId'}]},
        ],
        'relationships': relationships,
    }}


def _calendar_rel(**extra):
    rel = {'name': 'r1', 'fromTable': 'Orders', 'fromColumn': 'OrderDate',
           'toTable': 'Calendar', 'toColumn': 'Date', 'joinType': 'left'}
    rel.update(extra)
    return rel


class TestCalendarIsAlwaysTheOneSide(unittest.TestCase):

    def _run(self, rel, **model_kwargs):
        model = _model([rel], **model_kwargs)
        _detect_many_to_many(model, [])
        return model['model']['relationships'][0]

    def test_a_wide_calendar_is_not_a_peer_table(self):
        rel = self._run(_calendar_rel())
        self.assertEqual(rel['toCardinality'], 'one')
        self.assertEqual(rel['fromCardinality'], 'many')

    def test_it_holds_even_when_the_fact_table_is_tiny(self):
        rel = self._run(_calendar_rel(), fact_columns=1)
        self.assertEqual(rel['toCardinality'], 'one')

    def test_an_inferred_calendar_relationship_is_still_one(self):
        rel = self._run(_calendar_rel(name='inferred_date'))
        self.assertEqual(rel['toCardinality'], 'one')

    def test_a_full_join_to_calendar_is_still_one(self):
        # Cardinality describes the key's uniqueness, not the join type.
        rel = self._run(_calendar_rel(joinType='full'))
        self.assertEqual(rel['toCardinality'], 'one')

    def test_a_single_calendar_link_filters_one_way(self):
        rel = self._run(_calendar_rel())
        self.assertEqual(rel['crossFilteringBehavior'], 'oneDirection')

    def test_several_tables_make_calendar_a_bridge(self):
        model = _model([
            _calendar_rel(),
            _calendar_rel(name='r2', fromTable='Regions',
                          fromColumn='SignupDate'),
        ])
        _detect_many_to_many(model, [])
        for rel in model['model']['relationships']:
            self.assertEqual(rel['toCardinality'], 'one')
            self.assertEqual(rel['crossFilteringBehavior'], 'bothDirections')


class TestTheGuessesStillApplyElsewhere(unittest.TestCase):
    """Only Calendar is known; everything else stays conservative."""

    def _run(self, rel):
        model = _model([rel])
        _detect_many_to_many(model, [])
        return model['model']['relationships'][0]

    def test_a_peer_table_is_still_many_to_many(self):
        rel = self._run({'name': 'r', 'fromTable': 'Orders',
                         'fromColumn': 'RegionId', 'toTable': 'Orders',
                         'toColumn': 'RegionId', 'joinType': 'left'})
        self.assertEqual(rel['toCardinality'], 'many')

    def test_a_full_join_elsewhere_is_still_many_to_many(self):
        rel = self._run({'name': 'r', 'fromTable': 'Orders',
                         'fromColumn': 'RegionId', 'toTable': 'Regions',
                         'toColumn': 'RegionId', 'joinType': 'full'})
        self.assertEqual(rel['toCardinality'], 'many')

    def test_an_inferred_non_key_join_is_still_many_to_many(self):
        rel = self._run({'name': 'inferred_x', 'fromTable': 'Orders',
                         'fromColumn': 'Label', 'toTable': 'Regions',
                         'toColumn': 'RegionName', 'joinType': 'left'})
        self.assertEqual(rel['toCardinality'], 'many')


if __name__ == '__main__':
    unittest.main()
