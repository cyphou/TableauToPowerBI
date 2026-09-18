"""Parameter usage is not data blending.

Tableau exposes its parameter container as a pseudo-datasource, so any
worksheet referencing a parameter emits a dependency on `Parameters`. Counting
those as cross-datasource blends docked five workbooks for a feature none of
them used: measured across the example corpus, 15 of 15 extracted blend records
named `Parameters` as the secondary and none was a genuine blend.

`blend_graph` already owned that distinction and the assessment honoured it;
the parity detector did not, which is the divergence these tests pin.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.parity_registry import (  # noqa: E402
    _count_real_blends,
    scan_workbook,
)
from tableau_export.blend_graph import VIRTUAL_SECONDARIES  # noqa: E402


def _blend(primary, secondary, column='Region'):
    return {'datasource': primary, 'secondary_datasource': secondary,
            'column': column, 'link_expression': '', 'link_key': ''}


class TestRealBlendCount(unittest.TestCase):
    def test_parameter_dependency_is_not_a_blend(self):
        self.assertEqual(_count_real_blends({'data_blending': [
            _blend('Sales', 'Parameters', 'Parameter 1'),
            _blend('Sales', 'Parameters', 'Parameter 2'),
        ]}), 0)

    def test_genuine_blend_is_counted(self):
        self.assertEqual(_count_real_blends({'data_blending': [
            _blend('Sales', 'Targets'),
        ]}), 1)

    def test_self_reference_is_not_a_blend(self):
        self.assertEqual(_count_real_blends({'data_blending': [
            _blend('Sales', 'Sales'),
        ]}), 0)

    def test_link_marker_without_a_secondary_is_not_a_blend(self):
        """The link-field loop emits records with no secondary datasource."""
        self.assertEqual(_count_real_blends({'data_blending': [
            {'datasource': 'Sales', 'column': 'Region',
             'link_expression': '', 'link_key': ''},
        ]}), 0)

    def test_every_virtual_secondary_is_excluded(self):
        for secondary in VIRTUAL_SECONDARIES:
            with self.subTest(secondary=secondary):
                self.assertEqual(_count_real_blends({'data_blending': [
                    _blend('Sales', secondary)]}), 0)

    def test_case_is_ignored(self):
        self.assertEqual(_count_real_blends({'data_blending': [
            _blend('Sales', 'PARAMETERS')]}), 0)

    def test_mixed_list_counts_only_the_genuine_one(self):
        self.assertEqual(_count_real_blends({'data_blending': [
            _blend('Sales', 'Parameters', 'Parameter 1'),
            _blend('Sales', 'Targets'),
            _blend('Sales', 'Sales'),
        ]}), 1)

    def test_absent_and_malformed_input_is_safe(self):
        self.assertEqual(_count_real_blends({}), 0)
        self.assertEqual(_count_real_blends({'data_blending': None}), 0)


class TestBlendParityReflectsRealUse(unittest.TestCase):
    def test_parameters_only_workbook_reports_no_blend_family(self):
        scan = scan_workbook({'data_blending': [
            _blend('Sales', 'Parameters', 'Parameter 1')]})
        self.assertNotIn('data_blending', {u.key for u in scan.usages})

    def test_parameters_only_workbook_keeps_full_parity(self):
        scan = scan_workbook({'data_blending': [
            _blend('Sales', 'Parameters', 'Parameter 1')]})
        self.assertEqual(scan.parity_score, 100.0)

    def test_genuine_blend_is_still_reported_as_a_gap(self):
        scan = scan_workbook({'data_blending': [_blend('Sales', 'Targets')]})
        usage = {u.key: u for u in scan.usages}['data_blending']
        self.assertEqual(usage.status, 'approximated')
        self.assertEqual(usage.count, 1)

    def test_blending_stays_tracked_not_untracked(self):
        scan = scan_workbook({'data_blending': [
            _blend('Sales', 'Parameters', 'Parameter 1')]})
        self.assertNotIn('data_blending', scan.untracked_features)


if __name__ == '__main__':
    unittest.main()
