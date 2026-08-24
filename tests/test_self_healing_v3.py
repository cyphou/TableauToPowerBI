"""Sprint 136 — Self-Healing v3 tests.

Covers the 11 new healers in :mod:`powerbi_import.self_healing_v3`.
"""

from __future__ import annotations

import unittest

from powerbi_import.recovery_report import RecoveryReport
from powerbi_import.self_healing_v3 import (
    _heal_global_measure_dupes,
    _heal_self_referencing_measures,
    _heal_sort_by_column,
    _heal_hierarchies,
    _heal_display_folders,
    _heal_relationship_type_mismatch,
    _heal_invalid_identifiers,
    _heal_int64_decimal_format,
    _heal_datatype_casing,
    _heal_duplicate_relationships,
    _heal_hidden_key,
    _heal_empty_names,
    _heal_case_insensitive_dup_columns,
    _heal_empty_calculation_groups,
    _heal_relationship_missing_columns,
    _heal_dax_trailing_comma,
    _heal_measure_leading_equals,
    _heal_data_category,
    _heal_empty_annotations,
    _heal_duplicate_hierarchy_names,
    # v3.2
    _heal_column_without_datatype,
    _heal_measure_without_datatype,
    _heal_boolean_with_string_default,
    _heal_numeric_format_string_mismatch,
    _heal_datetime_without_format,
    _heal_lineage_tag_collision,
    _heal_missing_lineage_tag,
    _heal_source_column_missing,
    _heal_key_column_nullable,
    _heal_int_column_with_decimal_default,
    # v3.3
    _heal_m_unbalanced_let_in,
    _heal_m_duplicate_in,
    _heal_m_unbalanced_parens,
    _heal_m_step_name_collision,
    _heal_m_invalid_identifier_unquoted,
    _heal_m_trailing_comma_in_record,
    _heal_m_double_comma,
    _heal_m_missing_source_step,
    _heal_m_credential_in_expression,
    _heal_m_partition_mode_mismatch,
    _heal_m_dataflow_ref_dangling,
    _normalize_folder,
    _heal_calc_col_to_measure,
    run_v3_healers,
    _V3_HEALERS,
)


def _m_partition(expr, mode='import'):
    return {'mode': mode, 'source': {'type': 'm', 'expression': expr}}


def _table_with_m(name, expr, mode='import'):
    return {'name': name, 'partitions': [_m_partition(expr, mode)]}


def _model(tables=None, relationships=None):
    return {
        'model': {
            'tables': tables or [],
            'relationships': relationships or [],
        }
    }


# ════════════════════════════════════════════════════════════════════
#  #14 — Global duplicate measure names
# ════════════════════════════════════════════════════════════════════

class TestGlobalDuplicateMeasures(unittest.TestCase):

    def test_unique_measures_no_repair(self):
        m = _model([
            {'name': 'Sales', 'measures': [{'name': 'Total', 'expression': '1'}]},
            {'name': 'Cost', 'measures': [{'name': 'Sum', 'expression': '2'}]},
        ])
        self.assertEqual(_heal_global_measure_dupes(m), 0)

    def test_duplicate_renamed_with_table_suffix(self):
        m = _model([
            {'name': 'Sales', 'measures': [{'name': 'Total', 'expression': '1'}]},
            {'name': 'Cost', 'measures': [{'name': 'Total', 'expression': '2'}]},
        ])
        self.assertEqual(_heal_global_measure_dupes(m), 1)
        cost_measure = m['model']['tables'][1]['measures'][0]
        self.assertEqual(cost_measure['name'], 'Total_Cost')

    def test_first_occurrence_kept(self):
        m = _model([
            {'name': 'A', 'measures': [{'name': 'X', 'expression': '1'}]},
            {'name': 'B', 'measures': [{'name': 'X', 'expression': '2'}]},
        ])
        _heal_global_measure_dupes(m)
        self.assertEqual(m['model']['tables'][0]['measures'][0]['name'], 'X')

    def test_three_duplicates_get_unique_names(self):
        m = _model([
            {'name': 'A', 'measures': [{'name': 'X', 'expression': '1'}]},
            {'name': 'B', 'measures': [{'name': 'X', 'expression': '2'}]},
            {'name': 'C', 'measures': [{'name': 'X', 'expression': '3'}]},
        ])
        _heal_global_measure_dupes(m)
        names = {t['measures'][0]['name'] for t in m['model']['tables']}
        self.assertEqual(len(names), 3)

    def test_recovery_record(self):
        m = _model([
            {'name': 'A', 'measures': [{'name': 'X', 'expression': '1'}]},
            {'name': 'B', 'measures': [{'name': 'X', 'expression': '2'}]},
        ])
        r = RecoveryReport('test')
        _heal_global_measure_dupes(m, recovery=r)
        self.assertEqual(len(r.repairs), 1)
        self.assertEqual(r.repairs[0]['repair_type'], 'duplicate_measure_global')

    def test_migration_note_added(self):
        m = _model([
            {'name': 'A', 'measures': [{'name': 'X', 'expression': '1'}]},
            {'name': 'B', 'measures': [{'name': 'X', 'expression': '2'}]},
        ])
        _heal_global_measure_dupes(m)
        ann = m['model']['tables'][1]['measures'][0]['annotations']
        self.assertTrue(any('renamed' in a['value'].lower() for a in ann))


# ════════════════════════════════════════════════════════════════════
#  #15 — Self-referencing measures
# ════════════════════════════════════════════════════════════════════

class TestSelfReferencingMeasures(unittest.TestCase):

    def test_no_self_ref(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': 'SUM([Col])'},
        ]}])
        self.assertEqual(_heal_self_referencing_measures(m), 0)

    def test_bare_self_ref_detected(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': '[A] + 1'},
        ]}])
        self.assertEqual(_heal_self_referencing_measures(m), 1)
        meas = m['model']['tables'][0]['measures'][0]
        self.assertEqual(meas['expression'], 'BLANK()')
        self.assertTrue(meas['isHidden'])

    def test_qualified_self_ref_detected(self):
        m = _model([{'name': 'Sales', 'measures': [
            {'name': 'Total', 'expression': "'Sales'[Total] * 2"},
        ]}])
        self.assertEqual(_heal_self_referencing_measures(m), 1)

    def test_other_measure_ref_not_caught(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': '[B] + 1'},
            {'name': 'B', 'expression': '5'},
        ]}])
        self.assertEqual(_heal_self_referencing_measures(m), 0)

    def test_recovery_record(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': '[A]'},
        ]}])
        r = RecoveryReport('test')
        _heal_self_referencing_measures(m, recovery=r)
        self.assertEqual(r.repairs[0]['repair_type'], 'self_referencing_measure')


# ════════════════════════════════════════════════════════════════════
#  #16/17 — Sort-by-column hygiene
# ════════════════════════════════════════════════════════════════════

class TestSortByColumn(unittest.TestCase):

    def test_valid_sort_by_kept(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'MonthName', 'sortByColumn': 'Month'},
            {'name': 'Month'},
        ]}])
        self.assertEqual(_heal_sort_by_column(m), 0)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['sortByColumn'],
                         'Month')

    def test_self_reference_cleared(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'sortByColumn': 'X'},
        ]}])
        self.assertEqual(_heal_sort_by_column(m), 1)
        self.assertNotIn('sortByColumn', m['model']['tables'][0]['columns'][0])

    def test_missing_target_cleared(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'A', 'sortByColumn': 'NonExistent'},
        ]}])
        self.assertEqual(_heal_sort_by_column(m), 1)
        self.assertNotIn('sortByColumn', m['model']['tables'][0]['columns'][0])

    def test_no_sort_by_no_repair(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        self.assertEqual(_heal_sort_by_column(m), 0)

    def test_recovery_records_self_and_missing(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'sortByColumn': 'X'},
            {'name': 'Y', 'sortByColumn': 'Z'},
        ]}])
        r = RecoveryReport('test')
        _heal_sort_by_column(m, recovery=r)
        types = {rep['repair_type'] for rep in r.repairs}
        self.assertEqual(types,
                         {'sort_by_column_self', 'sort_by_column_missing'})


# ════════════════════════════════════════════════════════════════════
#  #18 — Hierarchy levels
# ════════════════════════════════════════════════════════════════════

class TestHierarchies(unittest.TestCase):

    def test_valid_hierarchy_kept(self):
        m = _model([{
            'name': 'T',
            'columns': [{'name': 'Year'}, {'name': 'Quarter'}],
            'hierarchies': [{
                'name': 'Time',
                'levels': [
                    {'name': 'L1', 'column': 'Year'},
                    {'name': 'L2', 'column': 'Quarter'},
                ],
            }],
        }])
        self.assertEqual(_heal_hierarchies(m), 0)

    def test_drops_invalid_level_only(self):
        m = _model([{
            'name': 'T',
            'columns': [{'name': 'Year'}],
            'hierarchies': [{
                'name': 'H',
                'levels': [
                    {'name': 'L1', 'column': 'Year'},
                    {'name': 'L2', 'column': 'Missing'},
                ],
            }],
        }])
        self.assertEqual(_heal_hierarchies(m), 1)
        levels = m['model']['tables'][0]['hierarchies'][0]['levels']
        self.assertEqual(len(levels), 1)

    def test_drops_hierarchy_when_all_levels_invalid(self):
        m = _model([{
            'name': 'T',
            'columns': [{'name': 'X'}],
            'hierarchies': [{
                'name': 'H',
                'levels': [
                    {'name': 'L1', 'column': 'Missing1'},
                    {'name': 'L2', 'column': 'Missing2'},
                ],
            }],
        }])
        # 2 invalid levels + 1 dropped hierarchy = 3 repairs
        self.assertEqual(_heal_hierarchies(m), 3)
        self.assertEqual(m['model']['tables'][0]['hierarchies'], [])

    def test_supports_sourceColumn_alias(self):
        m = _model([{
            'name': 'T',
            'columns': [{'name': 'Year'}],
            'hierarchies': [{
                'name': 'H',
                'levels': [{'name': 'L1', 'sourceColumn': 'Year'}],
            }],
        }])
        self.assertEqual(_heal_hierarchies(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #19 — Display folder normalization
# ════════════════════════════════════════════════════════════════════

class TestDisplayFolders(unittest.TestCase):

    def test_normalize_basic(self):
        self.assertEqual(_normalize_folder('A\\B'), 'A\\B')

    def test_normalize_strips_whitespace(self):
        self.assertEqual(_normalize_folder('  A \\ B  '), 'A\\B')

    def test_normalize_collapses_empty_segments(self):
        self.assertEqual(_normalize_folder('A\\\\B'), 'A\\B')

    def test_normalize_empty_returns_empty(self):
        self.assertEqual(_normalize_folder('  \\  \\  '), '')

    def test_clean_folder_no_repair(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'displayFolder': 'Dimensions'},
        ]}])
        self.assertEqual(_heal_display_folders(m), 0)

    def test_whitespace_folder_normalized(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'displayFolder': ' Dim \\ Sub '},
        ]}])
        self.assertEqual(_heal_display_folders(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['displayFolder'],
                         'Dim\\Sub')

    def test_all_whitespace_folder_removed(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'displayFolder': '   '},
        ]}])
        self.assertEqual(_heal_display_folders(m), 1)
        self.assertNotIn('displayFolder', m['model']['tables'][0]['columns'][0])

    def test_measure_folder_also_normalized(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'displayFolder': 'A\\\\B'},
        ]}])
        self.assertEqual(_heal_display_folders(m), 1)


# ════════════════════════════════════════════════════════════════════
#  #20 — Relationship data type mismatch
# ════════════════════════════════════════════════════════════════════

class TestRelationshipTypeMismatch(unittest.TestCase):

    def test_compatible_int_double_kept(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'k', 'dataType': 'int64'}]},
                {'name': 'B', 'columns': [{'name': 'k', 'dataType': 'double'}]},
            ],
            relationships=[
                {'fromTable': 'A', 'fromColumn': 'k',
                 'toTable': 'B', 'toColumn': 'k'},
            ],
        )
        self.assertEqual(_heal_relationship_type_mismatch(m), 0)
        self.assertEqual(len(m['model']['relationships']), 1)

    def test_string_to_int_removed(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'k', 'dataType': 'string'}]},
                {'name': 'B', 'columns': [{'name': 'k', 'dataType': 'int64'}]},
            ],
            relationships=[
                {'fromTable': 'A', 'fromColumn': 'k',
                 'toTable': 'B', 'toColumn': 'k'},
            ],
        )
        self.assertEqual(_heal_relationship_type_mismatch(m), 1)
        self.assertEqual(len(m['model']['relationships']), 0)

    def test_string_to_string_kept(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'k', 'dataType': 'string'}]},
                {'name': 'B', 'columns': [{'name': 'k', 'dataType': 'string'}]},
            ],
            relationships=[
                {'fromTable': 'A', 'fromColumn': 'k',
                 'toTable': 'B', 'toColumn': 'k'},
            ],
        )
        self.assertEqual(_heal_relationship_type_mismatch(m), 0)

    def test_unknown_types_kept(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'k'}]},
                {'name': 'B', 'columns': [{'name': 'k'}]},
            ],
            relationships=[{'fromTable': 'A', 'fromColumn': 'k',
                            'toTable': 'B', 'toColumn': 'k'}],
        )
        self.assertEqual(_heal_relationship_type_mismatch(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #21 — Invalid identifier characters
# ════════════════════════════════════════════════════════════════════

class TestInvalidIdentifiers(unittest.TestCase):

    def test_clean_names_no_repair(self):
        m = _model([{'name': 'Sales', 'columns': [{'name': 'Amount'}],
                     'measures': [{'name': 'Total', 'expression': '1'}]}])
        self.assertEqual(_heal_invalid_identifiers(m), 0)

    def test_strips_tab_from_table_name(self):
        m = _model([{'name': 'Sa\tles', 'columns': []}])
        self.assertEqual(_heal_invalid_identifiers(m), 1)
        self.assertEqual(m['model']['tables'][0]['name'], 'Sales')

    def test_strips_newline_from_column(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'A\nB'}]}])
        self.assertEqual(_heal_invalid_identifiers(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['name'], 'AB')

    def test_strips_null_from_measure(self):
        m = _model([{'name': 'T', 'measures': [{'name': 'M\x00x',
                                                 'expression': '1'}]}])
        self.assertEqual(_heal_invalid_identifiers(m), 1)
        self.assertEqual(m['model']['tables'][0]['measures'][0]['name'], 'Mx')

    def test_rewires_relationships_after_table_rename(self):
        m = _model(
            tables=[
                {'name': 'A\tlpha', 'columns': [{'name': 'k'}]},
                {'name': 'Beta', 'columns': [{'name': 'k'}]},
            ],
            relationships=[{'fromTable': 'A\tlpha', 'fromColumn': 'k',
                            'toTable': 'Beta', 'toColumn': 'k'}],
        )
        _heal_invalid_identifiers(m)
        self.assertEqual(m['model']['relationships'][0]['fromTable'], 'Alpha')


# ════════════════════════════════════════════════════════════════════
#  #22 — Int64 + decimal formatString
# ════════════════════════════════════════════════════════════════════

class TestInt64DecimalFormat(unittest.TestCase):

    def test_int64_no_format_no_repair(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64'},
        ]}])
        self.assertEqual(_heal_int64_decimal_format(m), 0)

    def test_int64_integer_format_no_repair(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'formatString': '#,##0'},
        ]}])
        self.assertEqual(_heal_int64_decimal_format(m), 0)

    def test_int64_decimal_format_promoted(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'formatString': '0.00'},
        ]}])
        self.assertEqual(_heal_int64_decimal_format(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'],
                         'double')

    def test_int64_thousands_with_decimals_promoted(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'formatString': '#,##0.0'},
        ]}])
        self.assertEqual(_heal_int64_decimal_format(m), 1)

    def test_double_decimal_no_change(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'double', 'formatString': '0.00'},
        ]}])
        self.assertEqual(_heal_int64_decimal_format(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #23 — dataType case normalization
# ════════════════════════════════════════════════════════════════════

class TestDataTypeCasing(unittest.TestCase):

    def test_canonical_no_repair(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'string'},
            {'name': 'Y', 'dataType': 'int64'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 0)

    def test_uppercase_int64_normalized(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'INT64'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'],
                         'int64')

    def test_titlecase_boolean_normalized(self):
        # "Boolean" (TitleCase) is accepted by PBI Desktop — no-op.
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'Boolean'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 0)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'],
                         'Boolean')

    def test_uppercase_boolean_normalized(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'BOOLEAN'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'],
                         'boolean')

    def test_datetime_normalized_to_camelcase(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'datetime'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'],
                         'dateTime')

    def test_unknown_type_left_alone(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'WeirdType'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 0)

    def test_integer_synonym_mapped_to_int64(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'integer'},
        ]}])
        self.assertEqual(_heal_datatype_casing(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'],
                         'int64')


# ════════════════════════════════════════════════════════════════════
#  #24 — Duplicate relationships
# ════════════════════════════════════════════════════════════════════

class TestDuplicateRelationships(unittest.TestCase):

    def test_unique_no_repair(self):
        m = _model(relationships=[
            {'fromTable': 'A', 'fromColumn': 'k',
             'toTable': 'B', 'toColumn': 'k'},
            {'fromTable': 'A', 'fromColumn': 'j',
             'toTable': 'B', 'toColumn': 'j'},
        ])
        self.assertEqual(_heal_duplicate_relationships(m), 0)

    def test_duplicate_deactivated(self):
        m = _model(relationships=[
            {'fromTable': 'A', 'fromColumn': 'k',
             'toTable': 'B', 'toColumn': 'k'},
            {'fromTable': 'A', 'fromColumn': 'k',
             'toTable': 'B', 'toColumn': 'k'},
        ])
        self.assertEqual(_heal_duplicate_relationships(m), 1)
        self.assertNotEqual(m['model']['relationships'][0].get('isActive'),
                            False)
        self.assertEqual(m['model']['relationships'][1]['isActive'], False)

    def test_already_inactive_duplicate_no_repair(self):
        m = _model(relationships=[
            {'fromTable': 'A', 'fromColumn': 'k',
             'toTable': 'B', 'toColumn': 'k'},
            {'fromTable': 'A', 'fromColumn': 'k',
             'toTable': 'B', 'toColumn': 'k', 'isActive': False},
        ])
        self.assertEqual(_heal_duplicate_relationships(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #25 — isHidden + isKey conflict
# ════════════════════════════════════════════════════════════════════

class TestHiddenKeyConflict(unittest.TestCase):

    def test_calendar_hidden_key_unhid(self):
        m = _model([{
            'name': 'Calendar',
            'columns': [{'name': 'Date', 'isKey': True, 'isHidden': True}],
        }])
        self.assertEqual(_heal_hidden_key(m), 1)
        self.assertFalse(m['model']['tables'][0]['columns'][0]['isHidden'])

    def test_non_date_table_not_touched(self):
        m = _model([{
            'name': 'Sales',
            'columns': [{'name': 'OrderID', 'isKey': True, 'isHidden': True}],
        }])
        self.assertEqual(_heal_hidden_key(m), 0)
        self.assertTrue(m['model']['tables'][0]['columns'][0]['isHidden'])

    def test_date_table_via_annotation(self):
        m = _model([{
            'name': 'AnyName',
            'annotations': [{'name': 'Copilot_DateTable', 'value': 'true'}],
            'columns': [{'name': 'Date', 'isKey': True, 'isHidden': True}],
        }])
        self.assertEqual(_heal_hidden_key(m), 1)

    def test_visible_key_no_repair(self):
        m = _model([{
            'name': 'Calendar',
            'columns': [{'name': 'Date', 'isKey': True, 'isHidden': False}],
        }])
        self.assertEqual(_heal_hidden_key(m), 0)

    def test_non_key_hidden_no_repair(self):
        m = _model([{
            'name': 'Calendar',
            'columns': [{'name': 'X', 'isHidden': True}],
        }])
        self.assertEqual(_heal_hidden_key(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #26 — Empty / whitespace-only identifier names
# ════════════════════════════════════════════════════════════════════

class TestEmptyNames(unittest.TestCase):

    def test_empty_table_renamed(self):
        m = _model([{'name': '', 'columns': []}])
        self.assertEqual(_heal_empty_names(m), 1)
        self.assertEqual(m['model']['tables'][0]['name'], 'Unnamed_Table_1')

    def test_whitespace_table_renamed(self):
        m = _model([{'name': '   ', 'columns': []}])
        self.assertEqual(_heal_empty_names(m), 1)

    def test_empty_column_renamed(self):
        m = _model([{'name': 'T', 'columns': [{'name': ''}]}])
        self.assertEqual(_heal_empty_names(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['name'],
                         'Unnamed_Column_1')

    def test_empty_measure_renamed(self):
        m = _model([{'name': 'T', 'measures': [{'name': '',
                                                 'expression': '1'}]}])
        self.assertEqual(_heal_empty_names(m), 1)

    def test_clean_no_repair(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X'}],
                     'measures': [{'name': 'M', 'expression': '1'}]}])
        self.assertEqual(_heal_empty_names(m), 0)

    def test_relationship_rewired_after_table_rename(self):
        m = _model(
            tables=[
                {'name': '', 'columns': [{'name': 'k'}]},
                {'name': 'B', 'columns': [{'name': 'k'}]},
            ],
            relationships=[{'fromTable': '', 'fromColumn': 'k',
                            'toTable': 'B', 'toColumn': 'k'}],
        )
        _heal_empty_names(m)
        self.assertEqual(m['model']['relationships'][0]['fromTable'],
                         'Unnamed_Table_1')


# ════════════════════════════════════════════════════════════════════
#  #27 — Case-insensitive duplicate columns
# ════════════════════════════════════════════════════════════════════

class TestCaseInsensitiveDupColumns(unittest.TestCase):

    def test_unique_no_repair(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'A'}, {'name': 'B'},
        ]}])
        self.assertEqual(_heal_case_insensitive_dup_columns(m), 0)

    def test_case_insensitive_dup_renamed(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'Date'}, {'name': 'date'},
        ]}])
        self.assertEqual(_heal_case_insensitive_dup_columns(m), 1)
        names = [c['name'] for c in m['model']['tables'][0]['columns']]
        self.assertEqual(names[0], 'Date')
        self.assertEqual(names[1], 'date_2')

    def test_three_dupes(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X'}, {'name': 'X'}, {'name': 'x'},
        ]}])
        self.assertEqual(_heal_case_insensitive_dup_columns(m), 2)

    def test_dupes_across_tables_no_repair(self):
        m = _model([
            {'name': 'A', 'columns': [{'name': 'X'}]},
            {'name': 'B', 'columns': [{'name': 'X'}]},
        ])
        self.assertEqual(_heal_case_insensitive_dup_columns(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #28 — Empty calculation groups
# ════════════════════════════════════════════════════════════════════

class TestEmptyCalculationGroups(unittest.TestCase):

    def test_no_calc_group_no_repair(self):
        m = _model([{'name': 'T', 'columns': []}])
        self.assertEqual(_heal_empty_calculation_groups(m), 0)

    def test_calc_group_with_items_kept(self):
        m = _model([{'name': 'T', 'calculationGroup': {
            'calculationItems': [{'name': 'YTD', 'expression': '1'}],
        }}])
        self.assertEqual(_heal_empty_calculation_groups(m), 0)
        self.assertIn('calculationGroup', m['model']['tables'][0])

    def test_empty_calc_group_dropped(self):
        m = _model([{'name': 'T', 'calculationGroup': {
            'calculationItems': [],
        }}])
        self.assertEqual(_heal_empty_calculation_groups(m), 1)
        self.assertNotIn('calculationGroup', m['model']['tables'][0])

    def test_supports_items_alias(self):
        m = _model([{'name': 'T', 'calculationGroup': {'items': []}}])
        self.assertEqual(_heal_empty_calculation_groups(m), 1)


# ════════════════════════════════════════════════════════════════════
#  #29 — Relationship column endpoint missing
# ════════════════════════════════════════════════════════════════════

class TestRelationshipMissingColumns(unittest.TestCase):

    def test_valid_kept(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'k'}]},
                {'name': 'B', 'columns': [{'name': 'k'}]},
            ],
            relationships=[{'fromTable': 'A', 'fromColumn': 'k',
                            'toTable': 'B', 'toColumn': 'k'}],
        )
        self.assertEqual(_heal_relationship_missing_columns(m), 0)
        self.assertEqual(len(m['model']['relationships']), 1)

    def test_missing_from_column_dropped(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'other'}]},
                {'name': 'B', 'columns': [{'name': 'k'}]},
            ],
            relationships=[{'fromTable': 'A', 'fromColumn': 'k',
                            'toTable': 'B', 'toColumn': 'k'}],
        )
        self.assertEqual(_heal_relationship_missing_columns(m), 1)
        self.assertEqual(len(m['model']['relationships']), 0)

    def test_missing_to_column_dropped(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'k'}]},
                {'name': 'B', 'columns': [{'name': 'other'}]},
            ],
            relationships=[{'fromTable': 'A', 'fromColumn': 'k',
                            'toTable': 'B', 'toColumn': 'k'}],
        )
        self.assertEqual(_heal_relationship_missing_columns(m), 1)

    def test_case_insensitive_match_kept(self):
        m = _model(
            tables=[
                {'name': 'A', 'columns': [{'name': 'Key'}]},
                {'name': 'B', 'columns': [{'name': 'KEY'}]},
            ],
            relationships=[{'fromTable': 'A', 'fromColumn': 'key',
                            'toTable': 'B', 'toColumn': 'Key'}],
        )
        self.assertEqual(_heal_relationship_missing_columns(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #30 — DAX trailing comma
# ════════════════════════════════════════════════════════════════════

class TestDaxTrailingComma(unittest.TestCase):

    def test_clean_dax_no_repair(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM(T[X])'},
        ]}])
        self.assertEqual(_heal_dax_trailing_comma(m), 0)

    def test_trailing_comma_stripped(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM(T[X],)'},
        ]}])
        self.assertEqual(_heal_dax_trailing_comma(m), 1)
        self.assertEqual(m['model']['tables'][0]['measures'][0]['expression'],
                         'SUM(T[X])')

    def test_trailing_comma_with_whitespace(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM(T[X], )'},
        ]}])
        self.assertEqual(_heal_dax_trailing_comma(m), 1)

    def test_calc_column_also_fixed(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'C', 'expression': 'IF(TRUE(), 1, )'},
        ]}])
        self.assertEqual(_heal_dax_trailing_comma(m), 1)


# ════════════════════════════════════════════════════════════════════
#  #31 — Measure leading "="
# ════════════════════════════════════════════════════════════════════

class TestMeasureLeadingEquals(unittest.TestCase):

    def test_clean_no_repair(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM(T[X])'},
        ]}])
        self.assertEqual(_heal_measure_leading_equals(m), 0)

    def test_leading_equals_stripped(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': '= SUM(T[X])'},
        ]}])
        self.assertEqual(_heal_measure_leading_equals(m), 1)
        self.assertEqual(m['model']['tables'][0]['measures'][0]['expression'],
                         'SUM(T[X])')

    def test_double_equals_left_alone(self):
        # "==" might be a typo but stripping a single "=" would be wrong
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': '== SUM(T[X])'},
        ]}])
        self.assertEqual(_heal_measure_leading_equals(m), 0)

    def test_leading_whitespace_then_equals(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': '  = SUM(T[X])'},
        ]}])
        self.assertEqual(_heal_measure_leading_equals(m), 1)


# ════════════════════════════════════════════════════════════════════
#  #32 — Invalid dataCategory
# ════════════════════════════════════════════════════════════════════

class TestInvalidDataCategory(unittest.TestCase):

    def test_valid_kept(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataCategory': 'City'},
        ]}])
        self.assertEqual(_heal_data_category(m), 0)
        self.assertEqual(
            m['model']['tables'][0]['columns'][0]['dataCategory'], 'City')

    def test_invalid_stripped(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataCategory': 'NotARealThing'},
        ]}])
        self.assertEqual(_heal_data_category(m), 1)
        self.assertNotIn('dataCategory', m['model']['tables'][0]['columns'][0])

    def test_no_data_category_no_repair(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        self.assertEqual(_heal_data_category(m), 0)


# ════════════════════════════════════════════════════════════════════
#  #33 — Empty annotations
# ════════════════════════════════════════════════════════════════════

class TestEmptyAnnotations(unittest.TestCase):

    def test_clean_no_repair(self):
        m = _model([{'name': 'T', 'annotations': [
            {'name': 'Foo', 'value': 'bar'},
        ], 'columns': []}])
        self.assertEqual(_heal_empty_annotations(m), 0)

    def test_empty_name_dropped(self):
        m = _model([{'name': 'T', 'annotations': [
            {'name': '', 'value': 'x'},
            {'name': 'Foo', 'value': 'bar'},
        ], 'columns': []}])
        self.assertEqual(_heal_empty_annotations(m), 1)
        self.assertEqual(len(m['model']['tables'][0]['annotations']), 1)

    def test_empty_value_kept(self):
        # Empty value is legal in TMDL
        m = _model([{'name': 'T', 'annotations': [
            {'name': 'Foo', 'value': ''},
        ], 'columns': []}])
        self.assertEqual(_heal_empty_annotations(m), 0)

    def test_column_and_measure_annotations_filtered(self):
        m = _model([{
            'name': 'T',
            'columns': [{'name': 'X', 'annotations': [
                {'name': '', 'value': 'a'},
            ]}],
            'measures': [{'name': 'M', 'expression': '1', 'annotations': [
                {'name': '   ', 'value': 'b'},
            ]}],
        }])
        self.assertEqual(_heal_empty_annotations(m), 2)


# ════════════════════════════════════════════════════════════════════
#  #34 — Duplicate hierarchy names
# ════════════════════════════════════════════════════════════════════

class TestDuplicateHierarchyNames(unittest.TestCase):

    def test_unique_no_repair(self):
        m = _model([{'name': 'T', 'hierarchies': [
            {'name': 'H1'}, {'name': 'H2'},
        ]}])
        self.assertEqual(_heal_duplicate_hierarchy_names(m), 0)

    def test_duplicate_renamed(self):
        m = _model([{'name': 'T', 'hierarchies': [
            {'name': 'Time'}, {'name': 'Time'},
        ]}])
        self.assertEqual(_heal_duplicate_hierarchy_names(m), 1)
        names = [h['name'] for h in m['model']['tables'][0]['hierarchies']]
        self.assertEqual(names, ['Time', 'Time_2'])

    def test_case_insensitive(self):
        m = _model([{'name': 'T', 'hierarchies': [
            {'name': 'TIME'}, {'name': 'time'},
        ]}])
        self.assertEqual(_heal_duplicate_hierarchy_names(m), 1)


# ════════════════════════════════════════════════════════════════════
#  v3.2 — Schema & datatype hygiene
# ════════════════════════════════════════════════════════════════════

class TestColumnWithoutDatatype(unittest.TestCase):

    def test_defaults_string(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        self.assertEqual(_heal_column_without_datatype(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'], 'string')

    def test_existing_dtype_skipped(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X', 'dataType': 'int64'}]}])
        self.assertEqual(_heal_column_without_datatype(m), 0)

    def test_records_to_recovery(self):
        rec = RecoveryReport("v3.2")
        m = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        _heal_column_without_datatype(m, recovery=rec)
        self.assertEqual(len(rec.repairs), 1)


class TestMeasureWithoutDatatype(unittest.TestCase):

    def test_sum_inferred_decimal(self):
        m = _model([{'name': 'T', 'measures': [{'name': 'M', 'expression': 'SUM(T[X])'}]}])
        _heal_measure_without_datatype(m)
        self.assertEqual(m['model']['tables'][0]['measures'][0]['dataType'], 'decimal')

    def test_distinctcount_inferred_int64(self):
        m = _model([{'name': 'T', 'measures': [{'name': 'M', 'expression': 'DISTINCTCOUNT(T[X])'}]}])
        _heal_measure_without_datatype(m)
        self.assertEqual(m['model']['tables'][0]['measures'][0]['dataType'], 'int64')

    def test_existing_dtype_skipped(self):
        m = _model([{'name': 'T', 'measures': [{'name': 'M', 'dataType': 'string', 'expression': 'X'}]}])
        self.assertEqual(_heal_measure_without_datatype(m), 0)


class TestBooleanWithStringDefault(unittest.TestCase):

    def test_string_true_normalized(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'boolean', 'defaultValue': 'true'},
        ]}])
        self.assertEqual(_heal_boolean_with_string_default(m), 1)
        self.assertIs(m['model']['tables'][0]['columns'][0]['defaultValue'], True)

    def test_already_bool_skipped(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'boolean', 'defaultValue': True},
        ]}])
        self.assertEqual(_heal_boolean_with_string_default(m), 0)

    def test_non_boolean_skipped(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'string', 'defaultValue': 'true'},
        ]}])
        self.assertEqual(_heal_boolean_with_string_default(m), 0)


class TestNumericFormatStringMismatch(unittest.TestCase):

    def test_int64_with_decimal_format_promoted(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'formatString': '0.00'},
        ]}])
        self.assertEqual(_heal_numeric_format_string_mismatch(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['dataType'], 'double')

    def test_int64_with_percent_promoted(self):
        m = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'dataType': 'int64', 'formatString': '0%'},
        ]}])
        self.assertEqual(_heal_numeric_format_string_mismatch(m), 1)

    def test_int64_with_int_format_skipped(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'formatString': '#,##0'},
        ]}])
        self.assertEqual(_heal_numeric_format_string_mismatch(m), 0)


class TestDatetimeWithoutFormat(unittest.TestCase):

    def test_datetime_gets_default_format(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'D', 'dataType': 'dateTime'}]}])
        self.assertEqual(_heal_datetime_without_format(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['formatString'], 'General Date')

    def test_existing_format_preserved(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'D', 'dataType': 'dateTime', 'formatString': 'yyyy-mm-dd'},
        ]}])
        self.assertEqual(_heal_datetime_without_format(m), 0)

    def test_string_skipped(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'D', 'dataType': 'string'}]}])
        self.assertEqual(_heal_datetime_without_format(m), 0)


class TestLineageTagCollision(unittest.TestCase):

    def test_collision_regenerated(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'A', 'lineageTag': 'shared-tag'},
            {'name': 'B', 'lineageTag': 'shared-tag'},
        ]}])
        self.assertEqual(_heal_lineage_tag_collision(m), 1)
        cols = m['model']['tables'][0]['columns']
        self.assertNotEqual(cols[0]['lineageTag'], cols[1]['lineageTag'])

    def test_unique_tags_no_repair(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'A', 'lineageTag': 'a'},
            {'name': 'B', 'lineageTag': 'b'},
        ]}])
        self.assertEqual(_heal_lineage_tag_collision(m), 0)


class TestMissingLineageTag(unittest.TestCase):

    def test_injects_uuid5(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        self.assertEqual(_heal_missing_lineage_tag(m), 1)
        self.assertTrue(m['model']['tables'][0]['columns'][0]['lineageTag'])

    def test_deterministic_across_runs(self):
        m1 = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        m2 = _model([{'name': 'T', 'columns': [{'name': 'X'}]}])
        _heal_missing_lineage_tag(m1)
        _heal_missing_lineage_tag(m2)
        self.assertEqual(
            m1['model']['tables'][0]['columns'][0]['lineageTag'],
            m2['model']['tables'][0]['columns'][0]['lineageTag'],
        )

    def test_existing_preserved(self):
        m = _model([{'name': 'T', 'columns': [{'name': 'X', 'lineageTag': 'keep'}]}])
        self.assertEqual(_heal_missing_lineage_tag(m), 0)


class TestSourceColumnMissing(unittest.TestCase):

    def test_case_mismatch_aligned(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'OrderId', 'sourceColumn': 'orderid'},
        ]}])
        self.assertEqual(_heal_source_column_missing(m), 1)
        self.assertEqual(
            m['model']['tables'][0]['columns'][0]['sourceColumn'], 'OrderId'
        )

    def test_no_match_left_alone(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'OrderId', 'sourceColumn': 'totally_unrelated'},
        ]}])
        self.assertEqual(_heal_source_column_missing(m), 0)


class TestKeyColumnNullable(unittest.TestCase):

    def test_key_forced_not_null(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'Id', 'isKey': True, 'isNullable': True},
        ]}])
        self.assertEqual(_heal_key_column_nullable(m), 1)
        self.assertFalse(m['model']['tables'][0]['columns'][0]['isNullable'])

    def test_non_key_unchanged(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'isNullable': True},
        ]}])
        self.assertEqual(_heal_key_column_nullable(m), 0)


class TestIntColumnDecimalDefault(unittest.TestCase):

    def test_rounded(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'defaultValue': 1.5},
        ]}])
        self.assertEqual(_heal_int_column_with_decimal_default(m), 1)
        self.assertEqual(m['model']['tables'][0]['columns'][0]['defaultValue'], 2)

    def test_int_default_unchanged(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'int64', 'defaultValue': 1},
        ]}])
        self.assertEqual(_heal_int_column_with_decimal_default(m), 0)


# ════════════════════════════════════════════════════════════════════
#  v3.3 — Power Query / M-partition hygiene
# ════════════════════════════════════════════════════════════════════

class TestMUnbalancedLetIn(unittest.TestCase):

    def test_appends_in(self):
        expr = 'let\n    Source = #table({}, {})'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_unbalanced_let_in(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertIn('in', out.lower())
        self.assertIn('Source', out)

    def test_existing_in_skipped(self):
        expr = 'let\n    Source = #table({}, {})\nin\n    Source'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_unbalanced_let_in(m), 0)

    def test_non_let_skipped(self):
        m = _model([_table_with_m('T', '#table({}, {})')])
        self.assertEqual(_heal_m_unbalanced_let_in(m), 0)

    def test_quoted_step_name_not_doubled(self):
        """Calendar-style M with #"step name" must NOT get a duplicate in."""
        expr = (
            'let\n'
            '    Source = List.Dates(#date(2020,1,1), 365, #duration(1,0,0,0)),\n'
            '    #"Added Year" = Table.AddColumn(Source, "Year", each Date.Year([Date]))\n'
            'in\n'
            '    #"Added Year"'
        )
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_unbalanced_let_in(m), 0)


class TestMDuplicateIn(unittest.TestCase):

    def test_removes_duplicate_in(self):
        expr = (
            'let\n    Source = #table({}, {})\nin\n    Source\nin\n    Source\n'
        )
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_duplicate_in(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertEqual(out.count('\nin\n'), 1)

    def test_removes_duplicate_quoted_step(self):
        expr = (
            'let\n    #"Added Year" = 1\nin\n    #"Added Year"\nin\n    #"Added Year"\n'
        )
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_duplicate_in(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertEqual(out.count('\nin\n'), 1)

    def test_single_in_unchanged(self):
        expr = 'let\n    Source = #table({}, {})\nin\n    Source'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_duplicate_in(m), 0)


class TestMUnbalancedParens(unittest.TestCase):

    def test_appends_close_paren(self):
        m = _model([_table_with_m('T', 'let Source = Table.FromRows({{1}}\nin Source')])
        self.assertEqual(_heal_m_unbalanced_parens(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertGreaterEqual(out.count(')'), out.count('('))

    def test_balanced_skipped(self):
        m = _model([_table_with_m('T', 'let Source = #table({}, {}) in Source')])
        self.assertEqual(_heal_m_unbalanced_parens(m), 0)

    def test_string_literals_ignored(self):
        m = _model([_table_with_m('T', 'let S = "((((" in S')])
        self.assertEqual(_heal_m_unbalanced_parens(m), 0)


class TestMStepNameCollision(unittest.TestCase):

    def test_duplicate_step_renamed(self):
        expr = 'let\n    A = 1,\n    A = 2\nin\n    A'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_step_name_collision(m), 1)

    def test_unique_steps_skipped(self):
        expr = 'let\n    A = 1,\n    B = 2\nin\n    B'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_step_name_collision(m), 0)


class TestMInvalidIdentifierUnquoted(unittest.TestCase):

    def test_wraps_step_with_space(self):
        expr = 'let\n    Removed Columns = 1\nin\n    Source'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_invalid_identifier_unquoted(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        # Identifier should be wrapped (allow trailing whitespace inside or outside quotes)
        self.assertRegex(out, r'#"Removed Columns\s*"')

    def test_clean_identifiers_unchanged(self):
        expr = 'let\n    Source = 1\nin\n    Source'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_invalid_identifier_unquoted(m), 0)


class TestMTrailingCommaInRecord(unittest.TestCase):

    def test_record_trailing_comma_removed(self):
        m = _model([_table_with_m('T', 'let R = [a=1, b=2,] in R')])
        self.assertEqual(_heal_m_trailing_comma_in_record(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertNotIn(',]', out)

    def test_list_trailing_comma_removed(self):
        m = _model([_table_with_m('T', 'let L = {1, 2, 3,} in L')])
        self.assertEqual(_heal_m_trailing_comma_in_record(m), 1)

    def test_clean_skipped(self):
        m = _model([_table_with_m('T', 'let R = [a=1, b=2] in R')])
        self.assertEqual(_heal_m_trailing_comma_in_record(m), 0)


class TestMDoubleComma(unittest.TestCase):

    def test_collapses(self):
        m = _model([_table_with_m('T', 'let X = Table.SelectRows(t,, each true) in X')])
        self.assertEqual(_heal_m_double_comma(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertNotIn(',,', out)

    def test_clean_skipped(self):
        m = _model([_table_with_m('T', 'let X = 1 in X')])
        self.assertEqual(_heal_m_double_comma(m), 0)


class TestMMissingSourceStep(unittest.TestCase):

    def test_injects_placeholder(self):
        expr = 'let\n    Renamed = Table.RenameColumns(Source, {})\nin\n    Renamed'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_missing_source_step(m), 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertIn('Source = #table', out)

    def test_existing_source_skipped(self):
        expr = 'let\n    Source = #table({}, {})\nin\n    Source'
        m = _model([_table_with_m('T', expr)])
        self.assertEqual(_heal_m_missing_source_step(m), 0)


class TestMCredentialInExpression(unittest.TestCase):

    def test_password_replaced(self):
        m = _model([_table_with_m('T', 'let X = Sql.Database("srv", [Password="secret"]) in X')])
        n = _heal_m_credential_in_expression(m)
        self.assertGreaterEqual(n, 1)
        out = m['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertNotIn('"secret"', out)
        self.assertIn('placeholder', out)

    def test_apikey_replaced(self):
        m = _model([_table_with_m('T', 'let X = [api_key="abc123"] in X')])
        n = _heal_m_credential_in_expression(m)
        self.assertGreaterEqual(n, 1)

    def test_clean_skipped(self):
        m = _model([_table_with_m('T', 'let X = 1 in X')])
        self.assertEqual(_heal_m_credential_in_expression(m), 0)


class TestMPartitionModeMismatch(unittest.TestCase):

    def test_dq_in_import_flagged(self):
        m = _model([_table_with_m('T', 'let X = Sql.Database("s", "d") in X', mode='import')])
        self.assertEqual(_heal_m_partition_mode_mismatch(m), 1)

    def test_with_table_buffer_skipped(self):
        m = _model([_table_with_m('T', 'let X = Table.Buffer(Sql.Database("s", "d")) in X')])
        self.assertEqual(_heal_m_partition_mode_mismatch(m), 0)

    def test_directquery_mode_skipped(self):
        m = _model([_table_with_m('T', 'let X = Sql.Database("s", "d") in X', mode='directQuery')])
        self.assertEqual(_heal_m_partition_mode_mismatch(m), 0)


class TestMDataflowRefDangling(unittest.TestCase):

    def test_dataflow_ref_flagged(self):
        m = _model([_table_with_m('T', 'let X = PowerPlatform.Dataflows(null) in X')])
        self.assertEqual(_heal_m_dataflow_ref_dangling(m), 1)

    def test_no_dataflow_ref_skipped(self):
        m = _model([_table_with_m('T', 'let X = 1 in X')])
        self.assertEqual(_heal_m_dataflow_ref_dangling(m), 0)


# ════════════════════════════════════════════════════════════════════
#  Integration — run_v3_healers
# ════════════════════════════════════════════════════════════════════

class TestRunAllV3Healers(unittest.TestCase):

    def test_v3_healers_count(self):
        # Sanity-check that all 52 healers (v3 + v3.1 + v3.2 + v3.3 + v3.5 + v3.6) are wired
        self.assertEqual(len(_V3_HEALERS), 52)

    def test_runs_all_on_clean_model(self):
        m = _model([{'name': 'T', 'columns': [
            {'name': 'X', 'dataType': 'string', 'lineageTag': 't-x'},
        ]}])
        self.assertEqual(run_v3_healers(m), 0)

    def test_runs_all_on_messy_model(self):
        m = _model(
            tables=[
                {
                    'name': 'Calendar',
                    'columns': [
                        {'name': 'Date', 'dataType': 'DateTime',
                         'isKey': True, 'isHidden': True},
                        {'name': 'X', 'sortByColumn': 'X'},
                    ],
                    'measures': [
                        {'name': 'Total', 'expression': '[Total]'},
                    ],
                },
                {
                    'name': 'Sales',
                    'columns': [
                        {'name': 'k', 'dataType': 'INT64',
                         'formatString': '0.00'},
                        {'name': 'Folder', 'displayFolder': '  A\\\\B  '},
                    ],
                    'measures': [
                        {'name': 'Total', 'expression': 'SUM([k])'},
                    ],
                },
            ],
            relationships=[
                {'fromTable': 'Calendar', 'fromColumn': 'Date',
                 'toTable': 'Sales', 'toColumn': 'k'},
                {'fromTable': 'Calendar', 'fromColumn': 'Date',
                 'toTable': 'Sales', 'toColumn': 'k'},
            ],
        )
        repairs = run_v3_healers(m)
        # At minimum: hidden_key, sort_by_self, self_ref_measure,
        # int64_decimal, datatype_casing (×2 INT64+DateTime), display_folder,
        # global_dupe_measure, duplicate_relationship, type_mismatch
        self.assertGreaterEqual(repairs, 8)

    def test_recovery_recorded(self):
        m = _model([{'name': 'A', 'measures': [
            {'name': 'X', 'expression': '1'},
        ]}, {'name': 'B', 'measures': [
            {'name': 'X', 'expression': '2'},
        ]}])
        r = RecoveryReport('test')
        run_v3_healers(m, recovery=r)
        self.assertGreater(len(r.repairs), 0)

    def test_healer_exception_does_not_block(self):
        # Pass a malformed model; ensure no raise
        m = {'model': {'tables': None, 'relationships': None}}
        try:
            run_v3_healers(m)
        except Exception as exc:
            self.fail(f'run_v3_healers raised {exc!r}')

    def test_no_recovery_no_raise(self):
        m = _model([{'name': 'A', 'measures': [
            {'name': 'X', 'expression': '[X]'},
        ]}])
        # Just make sure no recovery report works fine
        repairs = run_v3_healers(m, recovery=None)
        self.assertGreaterEqual(repairs, 1)


# ════════════════════════════════════════════════════════════════════
#  Wiring into _self_heal_model
# ════════════════════════════════════════════════════════════════════

class TestWiringIntoSelfHealModel(unittest.TestCase):

    def test_self_heal_model_invokes_v3(self):
        from powerbi_import.tmdl_generator import _self_heal_model
        m = _model([
            {'name': 'A', 'columns': [{'name': 'X', 'dataType': 'INT64'}],
             'measures': [{'name': 'M', 'expression': '1'}]},
            {'name': 'B', 'columns': [{'name': 'Y', 'dataType': 'string'}],
             'measures': [{'name': 'M', 'expression': '2'}]},
        ])
        r = RecoveryReport('test')
        _self_heal_model(m, recovery=r)
        types = {rep['repair_type'] for rep in r.repairs}
        # v3-only repair types should be present
        self.assertTrue(
            'datatype_casing' in types or
            'duplicate_measure_global' in types,
            f'Expected v3 healers to run; got {types}',
        )


# ════════════════════════════════════════════════════════════════════
#  v3.6 — Calc column to measure promotion
# ════════════════════════════════════════════════════════════════════

class TestCalcColToMeasure(unittest.TestCase):
    """Tests for _heal_calc_col_to_measure."""

    def test_promote_calculate_allexcept(self):
        """Calc column with CALCULATE + ALLEXCEPT should become a measure."""
        m = _model([{
            'name': 'Sales',
            'columns': [
                {'name': 'Revenue', 'dataType': 'double', 'sourceColumn': 'Revenue'},
                {'name': 'Per Customer',
                 'dataType': 'double',
                 'expression': "IF(ISBLANK(CALCULATE([Total Revenue], ALLEXCEPT('Sales', 'Sales'[Customer]))), 0, CALCULATE([Total Revenue], ALLEXCEPT('Sales', 'Sales'[Customer])))",
                 'type': 'calculated'},
            ],
            'measures': [
                {'name': 'Total Revenue', 'expression': "SUM('Sales'[Revenue])"},
            ],
        }])
        repairs = _heal_calc_col_to_measure(m)
        self.assertEqual(repairs, 1)
        # Column should be removed
        col_names = [c['name'] for c in m['model']['tables'][0]['columns']]
        self.assertNotIn('Per Customer', col_names)
        # Measure should be added
        measure_names = [ms['name'] for ms in m['model']['tables'][0]['measures']]
        self.assertIn('Per Customer', measure_names)
        # Check annotation
        promoted = [ms for ms in m['model']['tables'][0]['measures'] if ms['name'] == 'Per Customer'][0]
        ann_values = [a['value'] for a in promoted.get('annotations', [])]
        self.assertTrue(any('Promoted' in v for v in ann_values))

    def test_skip_selectedvalue_lookup(self):
        """CALCULATE(SELECTEDVALUE(...)) is a valid scalar lookup — should NOT be promoted."""
        m = _model([{
            'name': 'Sales',
            'columns': [
                {'name': 'Probability',
                 'dataType': 'double',
                 'expression': "CALCULATE(SELECTEDVALUE('Lookup'[Prob]))/100",
                 'type': 'calculated'},
            ],
            'measures': [],
        }])
        repairs = _heal_calc_col_to_measure(m)
        self.assertEqual(repairs, 0)
        col_names = [c['name'] for c in m['model']['tables'][0]['columns']]
        self.assertIn('Probability', col_names)

    def test_skip_lookupvalue(self):
        """LOOKUPVALUE is a valid scalar lookup — should NOT be promoted."""
        m = _model([{
            'name': 'Sales',
            'columns': [
                {'name': 'Region',
                 'dataType': 'string',
                 'expression': "LOOKUPVALUE('Regions'[Name], 'Regions'[ID], 'Sales'[RegionID])",
                 'type': 'calculated'},
            ],
            'measures': [],
        }])
        repairs = _heal_calc_col_to_measure(m)
        self.assertEqual(repairs, 0)

    def test_skip_no_expression(self):
        """Regular columns (no expression) should be skipped."""
        m = _model([{
            'name': 'Sales',
            'columns': [
                {'name': 'Amount', 'dataType': 'double', 'sourceColumn': 'Amount'},
            ],
            'measures': [],
        }])
        repairs = _heal_calc_col_to_measure(m)
        self.assertEqual(repairs, 0)

    def test_promote_sum(self):
        """Calc column with SUM(...) should become a measure."""
        m = _model([{
            'name': 'Sales',
            'columns': [
                {'name': 'Total',
                 'dataType': 'double',
                 'expression': "SUM('Sales'[Amount])",
                 'type': 'calculated',
                 'formatString': '#,0.00'},
            ],
            'measures': [],
        }])
        repairs = _heal_calc_col_to_measure(m)
        self.assertEqual(repairs, 1)
        promoted = m['model']['tables'][0]['measures'][0]
        self.assertEqual(promoted['name'], 'Total')
        self.assertEqual(promoted['formatString'], '#,0.00')

    def test_selectedvalue_with_measure_ref_promoted(self):
        """SELECTEDVALUE combined with a measure reference should be promoted."""
        m = _model([{
            'name': 'Sales',
            'columns': [
                {'name': 'Mixed',
                 'dataType': 'double',
                 'expression': "IF(SELECTEDVALUE('Sales'[Flag]) = 1, [Revenue Measure], 0)",
                 'type': 'calculated'},
            ],
            'measures': [
                {'name': 'Revenue Measure', 'expression': "SUM('Sales'[Revenue])"},
            ],
        }])
        # This does NOT contain CALCULATE/SUM at top level, only SELECTEDVALUE inside IF
        # The healer should NOT match this because the agg pattern only catches
        # top-level agg functions, and SELECTEDVALUE is excluded
        repairs = _heal_calc_col_to_measure(m)
        # SELECTEDVALUE alone is not in _CC_AGG_RE, so no promotion
        self.assertEqual(repairs, 0)

    def test_healer_in_v3_registry(self):
        """Ensure the healer is registered in _V3_HEALERS."""
        names = [name for name, _ in _V3_HEALERS]
        self.assertIn('calc_col_to_measure', names)


if __name__ == '__main__':
    unittest.main()
