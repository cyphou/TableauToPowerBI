"""Phase 4 tests — self-healing v3.5 model-side healers (Sprint 144)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from powerbi_import.self_healing_v3 import (
    _heal_column_duplicate_name_case,
    _heal_column_invalid_datatype,
    _heal_dax_circular_dependency,
    _heal_dax_unbalanced_brackets,
    _heal_dax_unknown_function,
    _heal_parameter_default_out_of_domain,
    _heal_partition_empty_m,
    _heal_relationship_orphan_table,
    _heal_relationship_self_loop,
    _heal_rls_missing_table_permission,
    run_v3_healers,
)


def _model(tables=None, relationships=None, roles=None):
    m = {'model': {'tables': tables or [], 'relationships': relationships or []}}
    if roles:
        m['model']['roles'] = roles
    return m


# ──────────────────────────────────────────────────────────────────
#  Healer: dax_unbalanced_brackets
# ──────────────────────────────────────────────────────────────────

class TestDaxUnbalancedBrackets(unittest.TestCase):
    def test_balanced_no_change(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM([Amount])'}
        ]}])
        self.assertEqual(_heal_dax_unbalanced_brackets(model), 0)

    def test_missing_close_bracket(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM([Amount)'}
        ]}])
        self.assertEqual(_heal_dax_unbalanced_brackets(model), 1)
        self.assertIn(']', model['model']['tables'][0]['measures'][0]['expression'])

    def test_extra_close_bracket(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM([Amount]])'}
        ]}])
        self.assertEqual(_heal_dax_unbalanced_brackets(model), 1)
        expr = model['model']['tables'][0]['measures'][0]['expression']
        self.assertEqual(expr.count('['), expr.count(']'))

    def test_column_expression_also_healed(self):
        model = _model([{'name': 'T', 'columns': [
            {'name': 'C', 'expression': '[A + [B]'}
        ]}])
        self.assertEqual(_heal_dax_unbalanced_brackets(model), 1)

    def test_empty_expression_ignored(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': ''}
        ]}])
        self.assertEqual(_heal_dax_unbalanced_brackets(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: dax_unknown_function
# ──────────────────────────────────────────────────────────────────

class TestDaxUnknownFunction(unittest.TestCase):
    def test_makepoint_replaced(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'MAKEPOINT([Lat], [Lon])'}
        ]}])
        self.assertEqual(_heal_dax_unknown_function(model), 1)
        self.assertIn('BLANK()', model['model']['tables'][0]['measures'][0]['expression'])
        self.assertIn('TODO', model['model']['tables'][0]['measures'][0]['expression'])

    def test_script_bool_replaced(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SCRIPT_BOOL("x", [Col])'}
        ]}])
        self.assertEqual(_heal_dax_unknown_function(model), 1)

    def test_valid_function_untouched(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'M', 'expression': 'SUM([Sales])'}
        ]}])
        self.assertEqual(_heal_dax_unknown_function(model), 0)

    def test_no_measures(self):
        model = _model([{'name': 'T'}])
        self.assertEqual(_heal_dax_unknown_function(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: dax_circular_dependency
# ──────────────────────────────────────────────────────────────────

class TestDaxCircularDependency(unittest.TestCase):
    def test_circular_pair_broken(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': '[B] + 1'},
            {'name': 'B', 'expression': '[A] + 2'},
        ]}])
        repairs = _heal_dax_circular_dependency(model)
        self.assertGreaterEqual(repairs, 1)
        # At least one of them should be BLANK()
        exprs = [m['expression'] for m in model['model']['tables'][0]['measures']]
        self.assertTrue(any('BLANK()' in e for e in exprs))

    def test_no_circular(self):
        model = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': 'SUM([Sales])'},
            {'name': 'B', 'expression': '[A] * 2'},
        ]}])
        self.assertEqual(_heal_dax_circular_dependency(model), 0)

    def test_self_reference_not_circular(self):
        # Self-referencing measures are handled by another healer
        model = _model([{'name': 'T', 'measures': [
            {'name': 'A', 'expression': 'SUM([Sales])'},
        ]}])
        self.assertEqual(_heal_dax_circular_dependency(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: relationship_orphan_table
# ──────────────────────────────────────────────────────────────────

class TestRelationshipOrphanTable(unittest.TestCase):
    def test_orphan_removed(self):
        model = _model(
            tables=[{'name': 'Sales'}, {'name': 'Products'}],
            relationships=[{
                'fromTable': 'Sales', 'toTable': 'Ghost',
                'fromColumn': 'x', 'toColumn': 'y',
            }],
        )
        self.assertEqual(_heal_relationship_orphan_table(model), 1)
        self.assertEqual(len(model['model']['relationships']), 0)

    def test_valid_relationship_kept(self):
        model = _model(
            tables=[{'name': 'Sales'}, {'name': 'Products'}],
            relationships=[{
                'fromTable': 'Sales', 'toTable': 'Products',
                'fromColumn': 'pid', 'toColumn': 'id',
            }],
        )
        self.assertEqual(_heal_relationship_orphan_table(model), 0)
        self.assertEqual(len(model['model']['relationships']), 1)


# ──────────────────────────────────────────────────────────────────
#  Healer: relationship_self_loop
# ──────────────────────────────────────────────────────────────────

class TestRelationshipSelfLoop(unittest.TestCase):
    def test_self_loop_removed(self):
        model = _model(
            tables=[{'name': 'T'}],
            relationships=[{
                'fromTable': 'T', 'toTable': 'T',
                'fromColumn': 'id', 'toColumn': 'id',
            }],
        )
        self.assertEqual(_heal_relationship_self_loop(model), 1)
        self.assertEqual(len(model['model']['relationships']), 0)

    def test_same_table_different_columns_kept(self):
        model = _model(
            tables=[{'name': 'T'}],
            relationships=[{
                'fromTable': 'T', 'toTable': 'T',
                'fromColumn': 'parent_id', 'toColumn': 'id',
            }],
        )
        self.assertEqual(_heal_relationship_self_loop(model), 0)
        self.assertEqual(len(model['model']['relationships']), 1)


# ──────────────────────────────────────────────────────────────────
#  Healer: column_duplicate_name_case
# ──────────────────────────────────────────────────────────────────

class TestColumnDuplicateNameCase(unittest.TestCase):
    def test_case_duplicate_renamed(self):
        model = _model([{'name': 'T', 'columns': [
            {'name': 'Date'},
            {'name': 'date'},
        ]}])
        self.assertEqual(_heal_column_duplicate_name_case(model), 1)
        names = [c['name'] for c in model['model']['tables'][0]['columns']]
        self.assertEqual(len(set(n.lower() for n in names)), 2)

    def test_no_duplicates(self):
        model = _model([{'name': 'T', 'columns': [
            {'name': 'Date'},
            {'name': 'Amount'},
        ]}])
        self.assertEqual(_heal_column_duplicate_name_case(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: column_invalid_datatype
# ──────────────────────────────────────────────────────────────────

class TestColumnInvalidDatatype(unittest.TestCase):
    def test_valid_casing_ignored(self):
        """Casing normalization is done by datatype_casing healer, not this one."""
        model = _model([{'name': 'T', 'columns': [
            {'name': 'C', 'dataType': 'String'},
        ]}])
        self.assertEqual(_heal_column_invalid_datatype(model), 0)

    def test_unknown_defaults_to_string(self):
        model = _model([{'name': 'T', 'columns': [
            {'name': 'C', 'dataType': 'blob'},
        ]}])
        self.assertEqual(_heal_column_invalid_datatype(model), 1)
        self.assertEqual(model['model']['tables'][0]['columns'][0]['dataType'], 'string')

    def test_valid_type_unchanged(self):
        model = _model([{'name': 'T', 'columns': [
            {'name': 'C', 'dataType': 'int64'},
        ]}])
        self.assertEqual(_heal_column_invalid_datatype(model), 0)

    def test_empty_datatype_ignored(self):
        model = _model([{'name': 'T', 'columns': [
            {'name': 'C', 'dataType': ''},
        ]}])
        self.assertEqual(_heal_column_invalid_datatype(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: partition_empty_m
# ──────────────────────────────────────────────────────────────────

class TestPartitionEmptyM(unittest.TestCase):
    def test_empty_m_replaced(self):
        model = _model([{'name': 'T', 'partitions': [
            {'source': {'type': 'm', 'expression': ''}}
        ]}])
        self.assertEqual(_heal_partition_empty_m(model), 1)
        expr = model['model']['tables'][0]['partitions'][0]['source']['expression']
        self.assertIn('#table', expr)

    def test_null_m_replaced(self):
        model = _model([{'name': 'T', 'partitions': [
            {'source': {'type': 'm', 'expression': 'null'}}
        ]}])
        self.assertEqual(_heal_partition_empty_m(model), 1)

    def test_valid_m_untouched(self):
        model = _model([{'name': 'T', 'partitions': [
            {'source': {'type': 'm', 'expression': 'let\n    Source = 1\nin\n    Source'}}
        ]}])
        self.assertEqual(_heal_partition_empty_m(model), 0)

    def test_dax_partition_ignored(self):
        model = _model([{'name': 'T', 'partitions': [
            {'source': {'type': 'calculated', 'expression': ''}}
        ]}])
        self.assertEqual(_heal_partition_empty_m(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: parameter_default_out_of_domain
# ──────────────────────────────────────────────────────────────────

class TestParameterDefaultOutOfDomain(unittest.TestCase):
    def test_out_of_domain_corrected(self):
        model = _model([{'name': 'T', 'measures': [
            {
                'name': 'P',
                'expression': 'SELECTEDVALUE(T[P], "A")',
                'annotations': [
                    {'name': 'ParameterDefaultValue', 'value': 'Z'},
                    {'name': 'ParameterAllowableValues', 'value': 'A,B,C'},
                ],
            }
        ]}])
        self.assertEqual(_heal_parameter_default_out_of_domain(model), 1)
        anns = model['model']['tables'][0]['measures'][0]['annotations']
        default_ann = next(a for a in anns if a['name'] == 'ParameterDefaultValue')
        self.assertEqual(default_ann['value'], 'A')

    def test_valid_default_unchanged(self):
        model = _model([{'name': 'T', 'measures': [
            {
                'name': 'P',
                'expression': 'SELECTEDVALUE(T[P], "B")',
                'annotations': [
                    {'name': 'ParameterDefaultValue', 'value': 'B'},
                    {'name': 'ParameterAllowableValues', 'value': 'A,B,C'},
                ],
            }
        ]}])
        self.assertEqual(_heal_parameter_default_out_of_domain(model), 0)


# ──────────────────────────────────────────────────────────────────
#  Healer: rls_missing_table_permission
# ──────────────────────────────────────────────────────────────────

class TestRlsMissingTablePermission(unittest.TestCase):
    def test_empty_role_gets_placeholder(self):
        model = _model(
            tables=[{'name': 'Sales'}],
            roles=[{'name': 'Reader', 'tablePermissions': []}],
        )
        self.assertEqual(_heal_rls_missing_table_permission(model), 1)
        perms = model['model']['roles'][0]['tablePermissions']
        self.assertEqual(len(perms), 1)
        self.assertEqual(perms[0]['filterExpression'], 'TRUE()')

    def test_role_with_perms_unchanged(self):
        model = _model(
            tables=[{'name': 'Sales'}],
            roles=[{'name': 'Reader', 'tablePermissions': [
                {'name': 'Sales', 'filterExpression': '[Region] = "East"'}
            ]}],
        )
        self.assertEqual(_heal_rls_missing_table_permission(model), 0)

    def test_none_perms_healed(self):
        model = _model(
            tables=[{'name': 'Sales'}],
            roles=[{'name': 'Reader', 'tablePermissions': None}],
        )
        self.assertEqual(_heal_rls_missing_table_permission(model), 1)


# ──────────────────────────────────────────────────────────────────
#  Integration: all new healers run via run_v3_healers
# ──────────────────────────────────────────────────────────────────

class TestPhase4Integration(unittest.TestCase):
    def test_all_healers_run_without_error(self):
        model = _model(
            tables=[
                {'name': 'T', 'columns': [
                    {'name': 'A', 'dataType': 'string'},
                ], 'measures': [
                    {'name': 'M', 'expression': 'SUM([A])'},
                ], 'partitions': [
                    {'source': {'type': 'm', 'expression': 'let\n    S = 1\nin\n    S'}},
                ]},
            ],
            relationships=[],
        )
        count = run_v3_healers(model)
        self.assertIsInstance(count, int)

    def test_multiple_healers_fire(self):
        """Model with multiple issues: each healer should fix its own."""
        model = _model(
            tables=[{'name': 'T', 'columns': [
                {'name': 'C', 'dataType': 'blobtype'},
            ], 'measures': [
                {'name': 'M', 'expression': 'MAKEPOINT([Lat], [Lon])'},
            ], 'partitions': [
                {'source': {'type': 'm', 'expression': ''}},
            ]}],
            relationships=[{
                'fromTable': 'T', 'toTable': 'T',
                'fromColumn': 'id', 'toColumn': 'id',
            }],
        )
        count = run_v3_healers(model)
        # At least 3: invalid datatype, unknown function, empty m, self-loop
        self.assertGreaterEqual(count, 3)

    def test_clean_model_low_repairs(self):
        """A minimal clean model should produce very few (cosmetic) repairs."""
        model = _model(
            tables=[{'name': 'T', 'columns': [
                {'name': 'A', 'dataType': 'string', 'sourceColumn': 'A',
                 'lineageTag': 'abc-123'},
            ], 'measures': [
                {'name': 'M', 'expression': 'SUM([A])', 'lineageTag': 'def-456'},
            ], 'partitions': [
                {'name': 'P', 'source': {'type': 'm',
                 'expression': 'let\n    Source = #table({"A"}, {{1}})\nin\n    Source'}},
            ]}],
            relationships=[],
        )
        count = run_v3_healers(model)
        # Only cosmetic healers (display_folders etc.) may fire
        self.assertLessEqual(count, 2)


if __name__ == '__main__':
    unittest.main()
