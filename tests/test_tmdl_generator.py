"""
Unit tests for tmdl_generator.py — TMDL semantic model generation.

Tests utility functions, semantic model building, Calendar table,
perspectives, cultures, theme, and TMDL file writers.
"""

import io
import json
import ntpath
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from tests.native_import_test_helpers import forbid_native_payload_io

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.tmdl_generator import (
    _add_date_table,
    _audit_local_partition_sources,
    _build_semantic_model,
    _fail_unavailable_local_sources,
    _get_display_folder,
    _get_format_string,
    _is_date_table,
    _map_semantic_role_to_category,
    _quote_name,
    _rewrite_bare_measure_references,
    _safe_filename,
    _tmdl_datatype,
    _tmdl_summarize,
    _write_culture_tmdl,
    _write_database_tmdl,
    _write_model_tmdl,
    _write_perspectives_tmdl,
    _write_relationships_tmdl,
    _write_table_tmdl,
    _write_tmdl_files,
    generate_theme_json,
    generate_tmdl,
)


class TestCombinedGroupSourceRelationshipContract(unittest.TestCase):
    """A cross-table row key requires one active, non-inferred to-one path."""

    extra = {'groups': [{'name': 'Combined key', 'group_type': 'combined'}]}

    def model(self, relationships):
        return {'model': {'tables': [
            {'name': 'Events', 'columns': [
                {'name': 'Combined key', 'displayFolder': 'Groups',
                 'expression': 'RELATED(\'Lookup\'[Description Modification])'}]},
            {'name': 'Lookup', 'columns': [{'name': 'Description Modification'}]},
            {'name': 'Bridge', 'columns': [{'name': 'ID'}]},
        ], 'relationships': relationships}}

    def edge(self, home='Events', target='Lookup', **overrides):
        edge = {'name': f'source_{home}_{target}', 'fromTable': home,
                'toTable': target, 'fromCardinality': 'many',
                'toCardinality': 'one', 'isActive': True}
        edge.update(overrides)
        return edge

    def validate(self, model):
        from powerbi_import.tmdl_generator import _validate_combined_group_relationships
        return _validate_combined_group_relationships(model, self.extra)

    def test_unique_explicit_active_to_one_source_path_is_accepted(self):
        self.validate(self.model([self.edge()]))
        self.validate(self.model([
            self.edge(target='Bridge'), self.edge(home='Bridge')]))

    def test_explicit_to_one_combined_group_processing_retargets_and_validates(self):
        from powerbi_import.tmdl_dax_postprocess import retarget_dax_column_refs
        from powerbi_import.tmdl_sets import _process_sets_groups_bins

        model = self.model([self.edge()])
        model['model']['tables'][0]['columns'] = [{'name': 'Event ID'}]
        model['_column_rename_map'] = {
            ('Lookup', '1006description_modification'): 'Description Modification'}
        extra = {'groups': [{
            'name': 'Combined key', 'group_type': 'combined',
            'source_fields': ['Event ID', '1006description_modification'],
        }]}
        _process_sets_groups_bins(model, extra, 'Events', {
            'Event ID': 'Events', '1006description_modification': 'Lookup'})
        retarget_dax_column_refs(model)
        self.validate(model)
        groups = [column for column in model['model']['tables'][0]['columns']
                  if column['name'] == 'Combined key']
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]['displayFolder'], 'Groups')
        self.assertIn("RELATED('Lookup'[Description Modification])",
                      groups[0]['expression'])
        self.assertIn("'Events'[Event ID]", groups[0]['expression'])
        self.assertNotIn('1006description_modification', groups[0]['expression'])
        self.assertNotIn('LOOKUPVALUE', groups[0]['expression'])

    def test_missing_inferred_inactive_full_and_many_to_many_paths_are_rejected(self):
        for relationships in (
            [], [self.edge(name='inferred_Events_Lookup')],
            [self.edge(isActive=False)], [self.edge(joinType='full')],
            [self.edge(toCardinality='many')],
        ):
            with self.subTest(relationships=relationships):
                with self.assertRaisesRegex(ValueError, 'unambiguous active to-one source'):
                    self.validate(self.model(relationships))

    def test_multiple_explicit_to_one_paths_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'unambiguous active to-one source'):
            self.validate(self.model([
                self.edge(), self.edge(target='Bridge'), self.edge(home='Bridge')]))

    def test_wrong_direction_many_to_one_path_does_not_prove_reverse_scalar_access(self):
        with self.assertRaisesRegex(ValueError, 'unambiguous active to-one source'):
            self.validate(self.model([self.edge(home='Lookup', target='Events')]))

    def test_missing_target_column_is_rejected_even_with_a_unique_path(self):
        model = self.model([self.edge()])
        model['model']['tables'][1]['columns'] = [{'name': 'Other'}]
        with self.assertRaisesRegex(ValueError, 'missing fields'):
            self.validate(model)

    def test_quoted_related_text_is_not_a_cross_table_dependency(self):
        model = self.model([])
        model['model']['tables'][0]['columns'][0]['expression'] = (
            '"RELATED(\'Missing\'[Not a column])"')
        self.validate(model)

    def test_cardinality_demotion_is_rejected_on_revalidation(self):
        model = self.model([self.edge()])
        self.validate(model)
        model['model']['relationships'][0]['toCardinality'] = 'many'
        with self.assertRaisesRegex(ValueError, 'many-to-many'):
            self.validate(model)

    def test_table_scoped_group_retarget_is_canonical_and_idempotent(self):
        from copy import deepcopy

        from powerbi_import.tmdl_dax_postprocess import retarget_dax_column_refs

        model = self.model([self.edge()])
        model['_column_rename_map'] = {
            ('Lookup', '1006description_modification'): 'Description Modification',
            ('Events', '1006description_modification'): 'Event description',
        }
        group = model['model']['tables'][0]['columns'][0]
        group['expression'] = (
            'RELATED(\'Lookup\'[1006description_modification]) & '
            '"[1006description_modification]"')
        self.assertEqual(retarget_dax_column_refs(model), 1)
        self.assertEqual(group['expression'],
                         'RELATED(\'Lookup\'[Description Modification]) & '
                         '"[1006description_modification]"')
        self.validate(model)
        before = deepcopy(model)
        self.assertEqual(retarget_dax_column_refs(model), 0)
        self.validate(model)
        self.assertEqual(model, before)

    def test_single_table_combined_key_uses_partition_caption_bindings(self):
        from copy import deepcopy

        from powerbi_import.tmdl_dax_postprocess import retarget_dax_column_refs
        from powerbi_import.tmdl_sets import _process_sets_groups_bins

        bindings = {
            '1006description_modification': 'Description Modification',
            '1007code_modification': 'Code Modification',
        }
        datasource = {
            'name': 'Synthetic',
            'tables': [{'name': 'Events', 'columns': [
                {'name': raw, 'datatype': 'string'} for raw in bindings]}],
            'columns': [
                {'name': f'[{raw}]', 'caption': caption, 'datatype': 'string'}
                for raw, caption in bindings.items()],
        }
        extra = {'groups': [{
            'name': 'Combined key', 'group_type': 'combined',
            'source_fields': list(bindings),
        }]}
        model = _build_semantic_model([datasource], 'Synthetic', extra)
        table = model['model']['tables'][0]
        self.assertEqual(model['_column_rename_map'], {
            ('Events', raw): caption for raw, caption in bindings.items()})
        self.assertEqual([col['name'] for col in table['columns']],
                         [*bindings.values(), 'Combined key'])
        partition = table['partitions'][0]['source']['expression']
        for raw, caption in bindings.items():
            self.assertIn(f'{{"{raw}", "{caption}"}}', partition)
            self.assertIn(f'[#"{caption}"]', partition)
            self.assertNotIn(f'[{raw}]', partition)
        before = deepcopy(model)
        _process_sets_groups_bins(
            model, extra, 'Events', {raw: 'Events' for raw in bindings})
        self.assertEqual(retarget_dax_column_refs(model), 0)
        self.assertEqual(model, before)


class TestCalculationIdentitySemanticContext(unittest.TestCase):
    """Fallback identifier delimiters must not become display-name brackets."""

    def context(self, calculations):
        from powerbi_import.tmdl_generator import _collect_semantic_context
        return _collect_semantic_context([{
            'name': 'Synthetic',
            'tables': [{
                'name': 'table',
                'columns': [{'name': 'Mail', 'datatype': 'string'}],
            }],
            'calculations': calculations,
        }], {})

    def test_repeated_bracketed_fallback_uses_canonical_constant_identity(self):
        context = self.context([{
            'name': '[Number of Records]',
            'caption': '[Number of Records]',
            'formula': '1',
        }])
        dax = context['dax_context']
        self.assertEqual(dax['param_values'], {'Number of Records': '1'})
        self.assertEqual(dax['measure_names'], {'Number of Records'})
        self.assertEqual(context['measure_names'], {'Number of Records'})
        self.assertEqual(dax['calc_map'], {})
        self.assertNotIn('[Number of Records]', dax['param_values'])
        self.assertNotIn('[Number of Records]', dax['measure_names'])

    def test_fallback_constant_reference_compiles_through_valid_dax_to_m(self):
        from powerbi_import.dax_validator import validate_dax_expression
        from powerbi_import.tmdl_m_conversion import _dax_to_m_expression
        from tableau_export.dax_converter import convert_tableau_formula_to_dax

        context = self.context([{
            'name': '[Number of Records]',
            'caption': '[Number of Records]',
            'formula': '1',
        }])['dax_context']
        dax = convert_tableau_formula_to_dax(
            'IF [Mail]="Mail" THEN [Number of Records] END',
            column_name='Mails renseignés', table_name='table',
            is_calc_column=True, validate_output=True, fallback_on_invalid=True,
            **{key: context[key] for key in (
                'calc_map', 'param_map', 'column_table_map',
                'measure_names', 'param_values')})
        self.assertEqual(dax, 'IF(\'table\'[Mail]="Mail", 1, BLANK())')
        self.assertEqual(validate_dax_expression(dax), [])
        self.assertEqual(_dax_to_m_expression(dax, 'table'),
                         'if [Mail] = "Mail" then 1 else null')

    def test_absent_empty_and_canonical_captions_share_the_same_identity(self):
        for caption in (None, '', 'Number of Records', '[Number of Records]'):
            with self.subTest(caption=caption):
                calculation = {'name': '[Number of Records]', 'formula': '1'}
                if caption is not None:
                    calculation['caption'] = caption
                context = self.context([calculation])['dax_context']
                self.assertEqual(context['calc_map'], {})
                self.assertEqual(context['param_values'], {'Number of Records': '1'})
                self.assertEqual(context['measure_names'], {'Number of Records'})

    def test_explicit_accented_and_punctuation_captions_are_preserved(self):
        context = self.context([
            {'name': '[Calculation_101]', 'caption': 'Écart %', 'formula': '1'},
            {'name': '[Calculation_202]', 'caption': 'CA/Qté', 'formula': '2'},
        ])['dax_context']
        self.assertEqual(context['calc_map'], {
            'Calculation_101': 'Écart %', 'Calculation_202': 'CA/Qté'})
        self.assertEqual(context['param_values'], {'Écart %': '1', 'CA/Qté': '2'})
        self.assertEqual(context['measure_names'], {'Écart %', 'CA/Qté'})

    def test_genuine_bracket_symbols_in_different_explicit_captions_survive(self):
        context = self.context([
            {'name': '[Calculation_101]', 'caption': '[Budget]', 'formula': '1'},
            {'name': '[Calculation_202]', 'caption': 'CA [HT]', 'formula': '2'},
        ])['dax_context']
        self.assertEqual(context['calc_map'], {
            'Calculation_101': '[Budget]', 'Calculation_202': 'CA [HT]'})
        self.assertEqual(context['param_values'], {'[Budget]': '1', 'CA [HT]': '2'})
        self.assertEqual(context['measure_names'], {'[Budget]', 'CA [HT]'})
        self.assertNotIn('Budget', context['param_values'])
        self.assertNotIn('CA HT', context['measure_names'])

    def test_fallback_removes_only_one_outer_pair_not_literal_inner_brackets(self):
        context = self.context([{
            'name': '[Metric [raw]]', 'caption': '[Metric [raw]]', 'formula': '1',
        }])['dax_context']
        self.assertEqual(context['calc_map'], {})
        self.assertEqual(context['param_values'], {'Metric [raw]': '1'})
        self.assertEqual(context['measure_names'], {'Metric [raw]'})


class TestLogicalRelationshipIntegration(unittest.TestCase):
    """Pair compilation must reach actual model measures before generic repair."""

    def setUp(self):
        from tests.logical_relationship_fixtures import (
            safe_policy,
            synthetic_datasource,
        )
        self.ds = synthetic_datasource()
        self.policy = safe_policy()
        self.environment = patch.dict(os.environ, {
            "TTPBI_LOGICAL_RELATIONSHIP_POLICIES": "{}",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def build(self, **extra):
        options = {"logical_relationship_policies": {"Synthetic": self.policy}}
        options.update(extra)
        return _build_semantic_model([self.ds], "Synthetic report", options)

    def measure(self, model, name):
        matches = [(table, measure) for table in model["model"]["tables"]
                   for measure in table.get("measures", [])
                   if measure["name"] == name]
        self.assertEqual(len(matches), 1, (name, matches))
        return matches[0]

    def test_actual_model_expression_executes_pair_bag_without_ordinary_inflation(self):
        from tests.logical_relationship_fixtures import EmittedDax, bag_rows
        model = self.build()
        table, measure = self.measure(model, "Pair total")
        self.assertEqual(table["name"], "Current")
        self.assertEqual(EmittedDax(measure["expression"]).evaluate(bag_rows()), 4)
        ordinary_table, ordinary = self.measure(model, "Ordinary amount")
        self.assertEqual(ordinary_table["name"], "Current")
        self.assertEqual(ordinary["expression"], "SUM('Current'[Amount])")
        # Ordinary measures keep their own row domain, not a flattened pair SUM.
        self.assertEqual(EmittedDax(ordinary["expression"]).evaluate(bag_rows()), 21)
        self.assertNotIn("__lr", ordinary["expression"])
        for table in model["model"]["tables"]:
            self.assertFalse({"Pair score", "Latest qualifying tick", "Pair total"}
                             & {c["name"] for c in table.get("columns", [])})
            self.assertFalse({"Pair score", "Latest qualifying tick"}
                             & {m["name"] for m in table.get("measures", [])})
        self.assertEqual(model["model"]["relationships"], [])

    def test_direct_visual_sum_of_row_calc_is_a_real_measure_not_calc_column(self):
        from tests.logical_relationship_fixtures import EmittedDax, bag_rows
        model = self.build(worksheets=[{
            "name": "Synthetic chart",
            "fields": [{"name": "row_score", "datasource": "Synthetic",
                        "aggregation": "sum"}],
        }])
        _, root = self.measure(model, "Pair total")
        table, direct = self.measure(model, "Pair score")
        self.assertEqual(table["name"], "Current")
        self.assertEqual(direct["expression"], root["expression"])
        self.assertEqual(EmittedDax(direct["expression"]).evaluate(bag_rows()), 4)
        self.assertNotIn("Pair score", [c["name"] for c in table["columns"]])

    def test_public_generation_persists_executable_pair_expression_without_healing_collapse(self):
        from tests.logical_relationship_fixtures import EmittedDax, bag_rows
        with tempfile.TemporaryDirectory() as output:
            stats = generate_tmdl(
                [self.ds], "Synthetic report",
                {"logical_relationship_policies": {"Synthetic": self.policy}},
                output_dir=output)
            path = os.path.join(output, "definition", "tables", "Current.tmdl")
            with open(path, encoding="utf-8") as stream:
                content = stream.read()
        self.assertEqual(stats["relationships"], 0)
        self.assertIn("\tmeasure 'Pair total' =", content)
        self.assertNotIn("\tcolumn 'Pair score'", content)
        # Read the serialized measure body, not the pre-write compiler result.
        body = content.split("\tmeasure 'Pair total' =", 1)[1]
        lines = []
        for line in body.splitlines()[1:]:
            if line.strip() == "```":
                break
            if line.startswith("\t\t\t"):
                lines.append(line[3:])
            elif lines:
                break
        self.assertTrue(lines, content)
        self.assertEqual(EmittedDax("\n".join(lines)).evaluate(bag_rows()), 4)

    def test_non_sum_visual_use_of_pair_scalar_fails_before_healing(self):
        for aggregation in ("", "max", "min", "avg", "attr"):
            with self.subTest(aggregation=aggregation):
                with self.assertRaisesRegex(ValueError, "unsupported direct visual use"):
                    self.build(worksheets=[{
                        "fields": [{"name": "row_score", "datasource": "Synthetic",
                                    "aggregation": aggregation}],
                    }])

    def test_independent_intermediate_dependency_fails_not_arbitrary_scalar(self):
        self.ds["calculations"].append(dict(
            name="[independent]", caption="Independent scalar", datatype="integer",
            formula="[row_score] + 1"))
        with self.assertRaisesRegex(ValueError, "independent model dependencies"):
            self.build()

    def test_absent_explicit_policy_fails_even_for_empty_filter_list(self):
        with self.assertRaisesRegex(ValueError, "requires explicit logical_relationship_policies"):
            _build_semantic_model([self.ds], extra_objects={"filters": []})

    def test_policy_requires_verified_case_encoding_and_four_scopes(self):
        for key in ("contains_case_sensitive", "comparison_case_sensitive",
                    "case_encoding_verified", "unmatched_left", "row_scopes"):
            with self.subTest(missing=key):
                policy = dict(self.policy)
                del policy[key]
                with self.assertRaisesRegex(ValueError, "requires explicit"):
                    self.build(logical_relationship_policies={"Synthetic": policy})
        self.policy["case_encoding_verified"] = False
        with self.assertRaisesRegex(ValueError, "requires explicit"):
            self.build()

    def test_environment_policy_and_explicit_extra_objects_have_same_expression(self):
        expected = self.measure(self.build(), "Pair total")[1]["expression"]
        with patch.dict(os.environ, {
            "TTPBI_LOGICAL_RELATIONSHIP_POLICIES": json.dumps({"Synthetic": self.policy}),
        }):
            model = _build_semantic_model([self.ds])
        self.assertEqual(self.measure(model, "Pair total")[1]["expression"], expected)

    def test_explicit_policy_overrides_environment_not_merged_with_it(self):
        with patch.dict(os.environ, {
            "TTPBI_LOGICAL_RELATIONSHIP_POLICIES": "not JSON",
        }):
            self.measure(self.build(), "Pair total")
        with patch.dict(os.environ, {
            "TTPBI_LOGICAL_RELATIONSHIP_POLICIES": json.dumps({"Synthetic": self.policy}),
        }):
            with self.assertRaisesRegex(ValueError, "requires explicit"):
                self.build(logical_relationship_policies={})

    def test_invalid_environment_policy_fails_explicitly(self):
        for raw, message in (("{", "Invalid TTPBI"), ("[]", "datasource-keyed")):
            with self.subTest(raw=raw), patch.dict(os.environ, {
                "TTPBI_LOGICAL_RELATIONSHIP_POLICIES": raw,
            }):
                with self.assertRaisesRegex(ValueError, message):
                    _build_semantic_model([self.ds])

    def test_explicit_original_relationship_survives_without_alias_inference(self):
        # Independent dimension relationship is real source evidence. The
        # non-equi Current/Prior relationship must NOT be replaced by a guessed
        # shared RowID, Entity, Bucket or Tick equality.
        self.ds["tables"].append({
            "name": "Buckets", "source_table": "[Buckets]",
            "columns": [{"name": "BucketID", "datatype": "string"}],
        })
        self.ds["relationships"] = [{
            "type": "left",
            "left": {"table": "Current", "column": "C_Group"},
            "right": {"table": "Buckets", "column": "BucketID"},
        }]
        model = self.build()
        relationships = model["model"]["relationships"]
        self.assertEqual(len(relationships), 1, relationships)
        rel = relationships[0]
        self.assertEqual((rel["fromTable"], rel["fromColumn"],
                          rel["toTable"], rel["toColumn"]),
                         ("Current", "Bucket", "Buckets", "BucketID"))
        self.assertFalse(rel["name"].startswith("inferred_"))

    def test_explicit_original_equality_between_same_source_aliases_is_not_suppressed(self):
        datasource = {
            "name": "Synthetic equality",
            "connection": self.ds["connection"],
            "tables": [
                {"name": alias, "source_table": "[Events]",
                 "columns": [{"name": "RowID", "datatype": "integer"}]}
                for alias in ("Current", "Prior")
            ],
            "relationships": [{
                "type": "left",
                "left": {"table": "Current", "column": "RowID"},
                "right": {"table": "Prior", "column": "RowID"},
            }],
        }
        model = _build_semantic_model([datasource])
        relationships = model["model"]["relationships"]
        self.assertEqual(len(relationships), 1, relationships)
        rel = relationships[0]
        self.assertEqual((rel["fromTable"], rel["fromColumn"],
                          rel["toTable"], rel["toColumn"]),
                         ("Current", "RowID", "Prior", "RowID"))
        self.assertFalse(rel["name"].startswith("inferred_"))


# ═══════════════════════════════════════════════════════════════════════
# Utility Functions (Pure — No I/O)
# ═══════════════════════════════════════════════════════════════════════

class TestQuoteName(unittest.TestCase):
    """Test _quote_name for TMDL identifier quoting."""

    def test_simple_name_no_quoting(self):
        self.assertEqual(_quote_name("Sales"), "Sales")

    def test_name_with_underscore(self):
        self.assertEqual(_quote_name("order_id"), "order_id")

    def test_name_with_spaces(self):
        self.assertEqual(_quote_name("Sales Data"), "'Sales Data'")

    def test_name_with_apostrophe(self):
        self.assertEqual(_quote_name("O'Brien"), "'O''Brien'")

    def test_name_with_special_chars(self):
        self.assertEqual(_quote_name("Sales (2024)"), "'Sales (2024)'")

    def test_name_with_hyphen(self):
        self.assertEqual(_quote_name("fact-table"), "'fact-table'")

    def test_name_with_dots(self):
        self.assertEqual(_quote_name("dbo.Sales"), "'dbo.Sales'")

    def test_empty_string(self):
        result = _quote_name("")
        self.assertEqual(result, "''")

    def test_numeric_only(self):
        self.assertEqual(_quote_name("123"), "'123'")


class TestTmdlDatatype(unittest.TestCase):
    """Test _tmdl_datatype type mapping."""

    def test_int64_capitals(self):
        self.assertEqual(_tmdl_datatype("Int64"), "int64")

    def test_int64_lowercase(self):
        self.assertEqual(_tmdl_datatype("int64"), "int64")

    def test_string(self):
        self.assertEqual(_tmdl_datatype("String"), "string")

    def test_double(self):
        self.assertEqual(_tmdl_datatype("Double"), "double")

    def test_datetime(self):
        self.assertEqual(_tmdl_datatype("DateTime"), "dateTime")

    def test_boolean(self):
        self.assertEqual(_tmdl_datatype("Boolean"), "boolean")

    def test_decimal(self):
        self.assertEqual(_tmdl_datatype("Decimal"), "decimal")

    def test_binary(self):
        self.assertEqual(_tmdl_datatype("Binary"), "binary")

    def test_unknown_defaults_to_string(self):
        self.assertEqual(_tmdl_datatype("SomeUnknownType"), "string")

    def test_empty_defaults_to_string(self):
        self.assertEqual(_tmdl_datatype(""), "string")


class TestTmdlSummarize(unittest.TestCase):
    """Test _tmdl_summarize mapping."""

    def test_sum(self):
        self.assertEqual(_tmdl_summarize("sum"), "sum")

    def test_none(self):
        self.assertEqual(_tmdl_summarize("none"), "none")

    def test_count(self):
        self.assertEqual(_tmdl_summarize("count"), "count")

    def test_unknown_defaults_to_none(self):
        self.assertEqual(_tmdl_summarize("unknown"), "none")

    def test_mixed_case(self):
        self.assertEqual(_tmdl_summarize("SUM"), "sum")


class TestSafeFilename(unittest.TestCase):
    """Test _safe_filename for stripping forbidden chars."""

    def test_normal_name(self):
        self.assertEqual(_safe_filename("Sales"), "Sales")

    def test_name_with_slash(self):
        self.assertEqual(_safe_filename("Sales/Data"), "Sales_Data")

    def test_name_with_colon(self):
        self.assertEqual(_safe_filename("Col:Name"), "Col_Name")

    def test_name_with_question_mark(self):
        self.assertEqual(_safe_filename("Sales?"), "Sales_")

    def test_name_with_all_forbidden(self):
        result = _safe_filename('<>:"/\\|?*')
        self.assertNotIn('<', result)
        self.assertNotIn('>', result)
        self.assertNotIn(':', result)
        self.assertNotIn('"', result)
        self.assertNotIn('/', result)
        self.assertNotIn('|', result)
        self.assertNotIn('?', result)
        self.assertNotIn('*', result)


class TestGetFormatString(unittest.TestCase):
    """Test _get_format_string format mapping."""

    def test_integer(self):
        self.assertEqual(_get_format_string("integer"), "0")

    def test_real(self):
        self.assertEqual(_get_format_string("real"), "#,0.00")

    def test_date(self):
        self.assertEqual(_get_format_string("date"), "Short Date")

    def test_datetime(self):
        self.assertEqual(_get_format_string("datetime"), "General Date")

    def test_boolean(self):
        self.assertEqual(_get_format_string("boolean"), "True/False")

    def test_percentage(self):
        self.assertEqual(_get_format_string("percentage"), "0.00%")

    def test_currency(self):
        self.assertEqual(_get_format_string("currency"), "$#,0.00")

    def test_unknown_defaults_to_zero(self):
        self.assertEqual(_get_format_string("unknown"), "0")


class TestGetDisplayFolder(unittest.TestCase):
    """Test _get_display_folder folder assignment."""

    def test_dimension_role(self):
        self.assertEqual(_get_display_folder("string", "dimension"), "Dimensions")

    def test_real_type(self):
        self.assertEqual(_get_display_folder("real", "measure"), "Measures")

    def test_integer_type(self):
        self.assertEqual(_get_display_folder("integer", ""), "Measures")

    def test_date_type(self):
        self.assertEqual(_get_display_folder("date", ""), "Time Intelligence")

    def test_datetime_type(self):
        self.assertEqual(_get_display_folder("datetime", ""), "Time Intelligence")

    def test_boolean_type(self):
        self.assertEqual(_get_display_folder("boolean", ""), "Flags")

    def test_string_defaults_to_calculations(self):
        self.assertEqual(_get_display_folder("string", ""), "Calculations")


class TestMapSemanticRole(unittest.TestCase):
    """Test _map_semantic_role_to_category geo role mapping."""

    def test_city_role(self):
        self.assertEqual(_map_semantic_role_to_category("[City].[Name]"), "City")

    def test_country_role(self):
        self.assertEqual(_map_semantic_role_to_category("[Country].[Name]"), "Country")

    def test_state_role(self):
        self.assertEqual(_map_semantic_role_to_category("[State].[Name]"), "StateOrProvince")

    def test_latitude_role(self):
        self.assertEqual(_map_semantic_role_to_category("[Latitude]"), "Latitude")

    def test_longitude_role(self):
        self.assertEqual(_map_semantic_role_to_category("[Longitude]"), "Longitude")

    def test_zipcode_role(self):
        self.assertEqual(_map_semantic_role_to_category("[ZipCode].[Name]"), "PostalCode")

    def test_no_role_city_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "city"), "City")

    def test_no_role_latitude_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "latitude"), "Latitude")

    def test_no_role_longitude_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "lng"), "Longitude")

    def test_no_role_country_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "country"), "Country")

    def test_no_role_postal_code_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "postal_code"), "PostalCode")

    def test_no_role_zip_code_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "zip"), "PostalCode")

    def test_no_role_region_col_name(self):
        self.assertEqual(_map_semantic_role_to_category("", "region"), "StateOrProvince")

    def test_no_match_returns_none(self):
        self.assertIsNone(_map_semantic_role_to_category("", "order_id"))

    def test_empty_both_returns_none(self):
        self.assertIsNone(_map_semantic_role_to_category(""))


# ═══════════════════════════════════════════════════════════════════════
# Theme Generation
# ═══════════════════════════════════════════════════════════════════════

class TestGenerateThemeJson(unittest.TestCase):
    """Test generate_theme_json theme builder."""

    def test_default_theme(self):
        theme = generate_theme_json()
        self.assertEqual(theme["name"], "Tableau Migration Theme")
        self.assertIn("dataColors", theme)
        self.assertEqual(len(theme["dataColors"]), 12)
        self.assertIn("textClasses", theme)
        self.assertIn("callout", theme["textClasses"])

    def test_custom_colors(self):
        theme = generate_theme_json({"colors": ["#FF0000", "#00FF00", "#0000FF"]})
        self.assertTrue(len(theme["dataColors"]) >= 3)
        self.assertIn("#FF0000", theme["dataColors"])

    def test_custom_font(self):
        theme = generate_theme_json({"font_family": "Arial"})
        self.assertEqual(theme["textClasses"]["callout"]["fontFace"], "Arial")

    def test_none_input(self):
        theme = generate_theme_json(None)
        self.assertIsInstance(theme, dict)
        self.assertIn("dataColors", theme)

    def test_empty_dict(self):
        theme = generate_theme_json({})
        self.assertIsInstance(theme, dict)
        self.assertEqual(len(theme["dataColors"]), 12)


# ═══════════════════════════════════════════════════════════════════════
# Semantic Model Building
# ═══════════════════════════════════════════════════════════════════════

class TestBuildSemanticModel(unittest.TestCase):
    """Test _build_semantic_model model creation."""

    def _build(self, *args, **kwargs):
        """Wrapper that redirects stdout to avoid cp1252 Unicode errors."""
        old_stdout = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        try:
            return _build_semantic_model(*args, **kwargs)
        finally:
            sys.stdout = old_stdout

    def _make_datasource(self, tables=None, calculations=None, relationships=None):
        """Helper to build a minimal datasource dict."""
        return {
            "name": "TestDS",
            "caption": "Test Datasource",
            "connection": {"type": "CSV", "details": {"filename": "test.csv"}},
            "tables": tables or [],
            "calculations": calculations or [],
            "columns": [],
            "relationships": relationships or [],
            "connection_map": {},
        }

    def _make_table(self, name="Orders", columns=None):
        """Helper to build a minimal table dict."""
        return {
            "name": name,
            "type": "table",
            "columns": columns or [
                {"name": "OrderID", "datatype": "integer"},
                {"name": "Amount", "datatype": "real"},
                {"name": "Date", "datatype": "datetime"},
            ],
        }

    def test_empty_datasources(self):
        model = self._build([], report_name="Empty")
        self.assertEqual(model["name"], "Empty")
        self.assertIn("model", model)

    def test_single_table(self):
        ds = self._make_datasource(tables=[self._make_table()])
        model = self._build([ds], report_name="Test")
        tables = model["model"]["tables"]
        # Should have Orders + Calendar (auto-generated from DateTime column)
        table_names = [t["name"] for t in tables]
        self.assertIn("Orders", table_names)

    def test_multiple_tables(self):
        ds = self._make_datasource(tables=[
            self._make_table("Orders"),
            self._make_table("Products", [
                {"name": "ProductID", "datatype": "integer"},
                {"name": "ProductName", "datatype": "string"},
            ]),
        ])
        model = self._build([ds])
        table_names = [t["name"] for t in model["model"]["tables"]]
        self.assertIn("Orders", table_names)
        self.assertIn("Products", table_names)

    def test_calculations_become_measures(self):
        ds = self._make_datasource(
            tables=[self._make_table()],
            calculations=[{
                "name": "[Calculation_001]",
                "caption": "Total Sales",
                "formula": "SUM([Amount])",
                "role": "measure",
                "datatype": "real",
            }],
        )
        model = self._build([ds])
        # Find the measure in the first table
        orders = [t for t in model["model"]["tables"] if t["name"] == "Orders"][0]
        measure_names = [m["name"] for m in orders.get("measures", [])]
        self.assertIn("Total Sales", measure_names)

    def test_date_table_auto_generated(self):
        ds = self._make_datasource(tables=[self._make_table()])
        model = self._build([ds])
        table_names = [t["name"] for t in model["model"]["tables"]]
        self.assertIn("Calendar", table_names)

    def test_no_date_table_if_no_datetime(self):
        ds = self._make_datasource(tables=[
            self._make_table("Products", [
                {"name": "ProductID", "datatype": "integer"},
                {"name": "ProductName", "datatype": "string"},
            ]),
        ])
        model = self._build([ds])
        table_names = [t["name"] for t in model["model"]["tables"]]
        self.assertNotIn("Calendar", table_names)

    def test_perspectives_auto_generated(self):
        ds = self._make_datasource(tables=[
            self._make_table("Orders"),
            self._make_table("Products", [
                {"name": "ProductID", "datatype": "integer"},
            ]),
        ])
        model = self._build([ds])
        perspectives = model["model"].get("perspectives", [])
        self.assertTrue(len(perspectives) >= 1)
        self.assertEqual(perspectives[0]["name"], "Full Model")

    def test_relationships_from_datasource(self):
        ds = self._make_datasource(
            tables=[
                self._make_table("Orders"),
                self._make_table("Customers", [
                    {"name": "CustomerID", "datatype": "integer"},
                    {"name": "Name", "datatype": "string"},
                ]),
            ],
            relationships=[{
                "join_type": "left",
                "from_table": "Orders",
                "to_table": "Customers",
                "from_column": "CustomerID",
                "to_column": "CustomerID",
                "raw_from_count": 1000,
                "raw_to_count": 100,
            }],
        )
        model = self._build([ds])
        rels = model["model"]["relationships"]
        # Should have at least the explicit relationship (+ possibly Calendar)
        self.assertTrue(len(rels) >= 1)

    def test_duplicate_table_deduplication(self):
        ds = self._make_datasource(tables=[
            self._make_table("Orders"),
            self._make_table("Orders"),  # duplicate
        ])
        model = self._build([ds])
        order_tables = [t for t in model["model"]["tables"] if t["name"] == "Orders"]
        self.assertEqual(len(order_tables), 1)


# ═══════════════════════════════════════════════════════════════════════
# Calendar / Date Table
# ═══════════════════════════════════════════════════════════════════════

class TestAddDateTable(unittest.TestCase):
    """Test _add_date_table Calendar auto-generation."""

    def test_adds_calendar_for_datetime_column(self):
        model = {
            "model": {
                "tables": [{
                    "name": "Sales",
                    "columns": [
                        {"name": "Date", "dataType": "dateTime", "sourceColumn": "Date"},
                        {"name": "Amount", "dataType": "double", "sourceColumn": "Amount"},
                    ],
                    "partitions": [],
                    "measures": [],
                }],
                "relationships": [],
            }
        }
        _add_date_table(model)
        table_names = [t["name"] for t in model["model"]["tables"]]
        self.assertIn("Calendar", table_names)

    def test_calendar_has_sortbycolumn(self):
        model = {
            "model": {
                "tables": [{
                    "name": "Sales",
                    "columns": [
                        {"name": "Date", "dataType": "dateTime", "sourceColumn": "Date"},
                    ],
                    "partitions": [],
                    "measures": [],
                }],
                "relationships": [],
            }
        }
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        month_name_col = [c for c in cal["columns"] if c["name"] == "MonthName"][0]
        day_name_col = [c for c in cal["columns"] if c["name"] == "DayName"][0]
        self.assertEqual(month_name_col.get("sortByColumn"), "Month")
        self.assertEqual(day_name_col.get("sortByColumn"), "DayOfWeek")

    def test_calendar_has_iskey(self):
        model = {
            "model": {
                "tables": [{
                    "name": "Sales",
                    "columns": [
                        {"name": "Date", "dataType": "dateTime", "sourceColumn": "Date"},
                    ],
                    "partitions": [],
                    "measures": [],
                }],
                "relationships": [],
            }
        }
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        date_col = [c for c in cal["columns"] if c["name"] == "Date"][0]
        self.assertTrue(date_col.get("isKey"))

    def test_calendar_creates_relationship(self):
        model = {
            "model": {
                "tables": [{
                    "name": "Sales",
                    "columns": [
                        {"name": "OrderDate", "dataType": "DateTime", "sourceColumn": "OrderDate"},
                    ],
                    "partitions": [],
                    "measures": [],
                }],
                "relationships": [],
            }
        }
        _add_date_table(model)
        rels = model["model"]["relationships"]
        cal_rels = [r for r in rels if "Calendar" in r.get("toTable", "")]
        self.assertTrue(len(cal_rels) >= 1)

    def test_no_calendar_relationship_without_datetime(self):
        """_add_date_table always adds Calendar, but no relationship without DateTime cols."""
        model = {
            "model": {
                "tables": [{
                    "name": "Products",
                    "columns": [
                        {"name": "Name", "dataType": "string", "sourceColumn": "Name"},
                    ],
                    "partitions": [],
                    "measures": [],
                }],
                "relationships": [],
            }
        }
        _add_date_table(model)
        # Calendar table is always added by _add_date_table
        table_names = [t["name"] for t in model["model"]["tables"]]
        self.assertIn("Calendar", table_names)
        # But no relationship should be created (no DateTime columns in Products)
        cal_rels = [r for r in model["model"]["relationships"] if "Calendar" in r.get("toTable", "")]
        self.assertEqual(len(cal_rels), 0)

    def _make_model_with_measure(self):
        """Helper: model with a DateTime column and a SUM measure."""
        return {
            "model": {
                "tables": [{
                    "name": "Sales",
                    "columns": [
                        {"name": "OrderDate", "dataType": "DateTime", "sourceColumn": "OrderDate"},
                        {"name": "Amount", "dataType": "double", "sourceColumn": "Amount"},
                    ],
                    "partitions": [],
                    "measures": [{"name": "Total Sales", "expression": "SUM('Sales'[Amount])"}],
                }],
                "relationships": [],
            }
        }

    def test_calendar_columns_complete(self):
        """All 8 expected Calendar columns are present."""
        model = self._make_model_with_measure()
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        col_names = {c["name"] for c in cal["columns"]}
        expected = {"Date", "Year", "Quarter", "Month", "MonthName", "Day", "DayOfWeek", "DayName"}
        self.assertEqual(col_names, expected)

    def test_calendar_partition_is_m(self):
        """Calendar uses an M partition (not DAX calculated)."""
        model = self._make_model_with_measure()
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        partition = cal["partitions"][0]
        self.assertEqual(partition["source"]["type"], "m")
        self.assertIn("List.Dates", partition["source"]["expression"])

    def test_calendar_data_categories(self):
        """Calendar columns have correct dataCategory."""
        model = self._make_model_with_measure()
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        col_map = {c["name"]: c for c in cal["columns"]}
        self.assertEqual(col_map["Date"].get("dataCategory"), "DateTime")
        self.assertEqual(col_map["Year"].get("dataCategory"), "Years")
        self.assertEqual(col_map["Month"].get("dataCategory"), "Months")
        self.assertEqual(col_map["Day"].get("dataCategory"), "Days")

    def test_calendar_time_intelligence_measures(self):
        """Calendar generates YTD, Previous Year, YoY% measures when SUM measure exists."""
        model = self._make_model_with_measure()
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        measure_names = {m["name"] for m in cal.get("measures", [])}
        self.assertIn("Year To Date", measure_names)
        self.assertIn("Previous Year", measure_names)
        self.assertIn("Year Over Year %", measure_names)

    def test_calendar_no_time_intelligence_without_sum(self):
        """No time intelligence measures when no SUM measure exists."""
        model = {
            "model": {
                "tables": [{
                    "name": "Sales",
                    "columns": [
                        {"name": "Date", "dataType": "DateTime", "sourceColumn": "Date"},
                    ],
                    "partitions": [],
                    "measures": [{"name": "Ratio", "expression": "DIVIDE([A],[B])"}],
                }],
                "relationships": [],
            }
        }
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        self.assertEqual(len(cal.get("measures", [])), 0)

    def test_calendar_hierarchy_levels_order(self):
        """Calendar hierarchy has Year→Quarter→Month→Day in correct order."""
        model = self._make_model_with_measure()
        _add_date_table(model)
        cal = [t for t in model["model"]["tables"] if t["name"] == "Calendar"][0]
        hierarchy = cal["hierarchies"][0]
        self.assertEqual(hierarchy["name"], "Date Hierarchy")
        levels = hierarchy["levels"]
        self.assertEqual(len(levels), 4)
        self.assertEqual(levels[0]["name"], "Year")
        self.assertEqual(levels[1]["name"], "Quarter")
        self.assertEqual(levels[2]["name"], "Month")
        self.assertEqual(levels[3]["name"], "Day")

    def test_calendar_not_duplicated(self):
        """Calling _add_date_table twice does not create duplicate Calendar tables."""
        model = self._make_model_with_measure()
        _add_date_table(model)
        _add_date_table(model)
        cal_tables = [t for t in model["model"]["tables"] if t["name"] == "Calendar"]
        self.assertEqual(len(cal_tables), 1)

    def test_skip_calendar_when_source_has_calendar_table(self):
        """If source data already has a 'Calendar' table, _add_date_table is a no-op."""
        model = {
            "model": {
                "tables": [
                    {
                        "name": "Calendar",
                        "columns": [
                            {"name": "Date", "dataType": "DateTime", "sourceColumn": "Date"},
                            {"name": "Year", "dataType": "int64", "sourceColumn": "Year"},
                        ],
                        "partitions": [{"name": "p", "mode": "import",
                                         "source": {"type": "m", "expression": "let Source = ..."}}],
                        "measures": [],
                    },
                    {
                        "name": "Sales",
                        "columns": [
                            {"name": "OrderDate", "dataType": "DateTime", "sourceColumn": "OrderDate"},
                        ],
                        "partitions": [],
                        "measures": [],
                    },
                ],
                "relationships": [],
            }
        }
        _add_date_table(model)
        cal_tables = [t for t in model["model"]["tables"] if t["name"] == "Calendar"]
        # Should remain exactly the original one — no auto-generated duplicate
        self.assertEqual(len(cal_tables), 1)
        # The original should NOT have been replaced (no 'Calendar-Partition' partition)
        self.assertEqual(cal_tables[0]["partitions"][0]["name"], "p")

    def test_skip_calendar_when_source_has_dimdate_table(self):
        """Source table named 'DimDate' is detected as a date table via _is_date_table."""
        table = {
            "name": "DimDate",
            "columns": [
                {"name": "DateKey", "dataType": "DateTime", "sourceColumn": "DateKey"},
                {"name": "Year", "dataType": "int64", "sourceColumn": "Year"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_skip_calendar_when_source_has_date_table(self):
        """Source table named 'Date' is detected as a date table."""
        table = {
            "name": "Date",
            "columns": [
                {"name": "FullDate", "dataType": "DateTime", "sourceColumn": "FullDate"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_skip_calendar_when_source_has_french_calendrier(self):
        """Source table named 'Calendrier' (French) is detected as a date table."""
        table = {
            "name": "Calendrier",
            "columns": [
                {"name": "Date", "dataType": "DateTime", "sourceColumn": "Date"},
                {"name": "Annee", "dataType": "int64", "sourceColumn": "Annee"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_skip_calendar_when_source_has_german_datum(self):
        """Source table named 'Datum' (German) is detected as a date table."""
        table = {
            "name": "Datum",
            "columns": [
                {"name": "Datum", "dataType": "DateTime", "sourceColumn": "Datum"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_skip_calendar_when_source_has_spanish_fecha(self):
        """Source table named 'DimFecha' (Spanish) is detected as a date table."""
        table = {
            "name": "DimFecha",
            "columns": [
                {"name": "Fecha", "dataType": "DateTime", "sourceColumn": "Fecha"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_heuristic_detects_custom_named_date_table(self):
        """A table with a custom name but date-like columns is detected by heuristic."""
        table = {
            "name": "MyCustomDateLookup",
            "columns": [
                {"name": "Date", "dataType": "DateTime", "sourceColumn": "Date"},
                {"name": "Year", "dataType": "int64", "sourceColumn": "Year"},
                {"name": "Month", "dataType": "int64", "sourceColumn": "Month"},
                {"name": "Quarter", "dataType": "string", "sourceColumn": "Quarter"},
                {"name": "Day", "dataType": "int64", "sourceColumn": "Day"},
                {"name": "DayName", "dataType": "string", "sourceColumn": "DayName"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_heuristic_detects_french_column_date_table(self):
        """A table with French date-part column names is detected by heuristic."""
        table = {
            "name": "TableTemporelle",
            "columns": [
                {"name": "Date", "dataType": "DateTime", "sourceColumn": "Date"},
                {"name": "Année", "dataType": "int64", "sourceColumn": "Année"},
                {"name": "Mois", "dataType": "int64", "sourceColumn": "Mois"},
                {"name": "Jour", "dataType": "int64", "sourceColumn": "Jour"},
                {"name": "Trimestre", "dataType": "string", "sourceColumn": "Trimestre"},
                {"name": "Semaine", "dataType": "int64", "sourceColumn": "Semaine"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_heuristic_does_not_flag_fact_table(self):
        """A fact table with a DateTime column but mostly non-date columns is NOT a date table."""
        table = {
            "name": "Sales",
            "columns": [
                {"name": "OrderDate", "dataType": "DateTime", "sourceColumn": "OrderDate"},
                {"name": "Amount", "dataType": "double", "sourceColumn": "Amount"},
                {"name": "Quantity", "dataType": "int64", "sourceColumn": "Quantity"},
                {"name": "Product", "dataType": "string", "sourceColumn": "Product"},
                {"name": "Customer", "dataType": "string", "sourceColumn": "Customer"},
                {"name": "Region", "dataType": "string", "sourceColumn": "Region"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertFalse(_is_date_table(table))

    def test_heuristic_german_columns(self):
        """German date-part column names detected by heuristic."""
        table = {
            "name": "ZeitDimension",
            "columns": [
                {"name": "Datum", "dataType": "DateTime", "sourceColumn": "Datum"},
                {"name": "Jahr", "dataType": "int64", "sourceColumn": "Jahr"},
                {"name": "Monat", "dataType": "int64", "sourceColumn": "Monat"},
                {"name": "Tag", "dataType": "int64", "sourceColumn": "Tag"},
                {"name": "Quartal", "dataType": "string", "sourceColumn": "Quartal"},
                {"name": "Woche", "dataType": "int64", "sourceColumn": "Woche"},
            ],
            "partitions": [], "measures": [],
        }
        self.assertTrue(_is_date_table(table))

    def test_calendar_multi_table_relationships(self):
        """Calendar links to date columns in multiple fact tables."""
        model = {
            "model": {
                "tables": [
                    {
                        "name": "Orders",
                        "columns": [
                            {"name": "OrderDate", "dataType": "DateTime", "sourceColumn": "OrderDate"},
                        ],
                        "partitions": [],
                        "measures": [],
                    },
                    {
                        "name": "Shipments",
                        "columns": [
                            {"name": "ShipDate", "dataType": "DateTime", "sourceColumn": "ShipDate"},
                        ],
                        "partitions": [],
                        "measures": [],
                    },
                ],
                "relationships": [],
            }
        }
        _add_date_table(model)
        cal_rels = [r for r in model["model"]["relationships"] if r.get("toTable") == "Calendar"]
        linked_tables = {r["fromTable"] for r in cal_rels}
        self.assertIn("Orders", linked_tables)
        self.assertIn("Shipments", linked_tables)


# ═══════════════════════════════════════════════════════════════════════
# TMDL File Writers (I/O — uses temp dirs)
# ═══════════════════════════════════════════════════════════════════════

class TestTmdlFileWriters(unittest.TestCase):
    """Test TMDL file writing functions."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_perspectives_tmdl(self):
        perspectives = [{"name": "Full Model", "tables": ["Orders", "Products"]}]
        _write_perspectives_tmdl(self.tmpdir, perspectives)
        path = os.path.join(self.tmpdir, "perspectives.tmdl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("perspective", content)
        self.assertIn("perspectiveTable Orders", content)
        self.assertIn("perspectiveTable Products", content)

    def test_write_perspectives_with_special_names(self):
        perspectives = [{"name": "Full Model", "tables": ["Sales Data", "O'Brien"]}]
        _write_perspectives_tmdl(self.tmpdir, perspectives)
        path = os.path.join(self.tmpdir, "perspectives.tmdl")
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("'Sales Data'", content)
        self.assertIn("'O''Brien'", content)

    def test_write_culture_tmdl(self):
        tables = [{"name": "Orders", "columns": [{"name": "Amount"}]}]
        _write_culture_tmdl(self.tmpdir, "fr-FR", tables)
        path = os.path.join(self.tmpdir, "fr-FR.tmdl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("culture", content)
        self.assertIn("fr-FR", content)
        self.assertIn("linguisticMetadata", content)

    def test_write_database_tmdl(self):
        _write_database_tmdl(self.tmpdir, {"compatibilityLevel": 1600})
        path = os.path.join(self.tmpdir, "database.tmdl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("database", content)
        self.assertIn("1600", content)

    def test_write_database_upgrades_compat_level(self):
        _write_database_tmdl(self.tmpdir, {"compatibilityLevel": 1400})
        with open(os.path.join(self.tmpdir, "database.tmdl"), "r") as f:
            content = f.read()
        # Should be clamped to at least 1600
        self.assertIn("1600", content)

    def test_write_model_tmdl_basic(self):
        model = {"culture": "en-US"}
        tables = [{"name": "Orders"}, {"name": "Products"}]
        _write_model_tmdl(self.tmpdir, model, tables)
        path = os.path.join(self.tmpdir, "model.tmdl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("model Model", content)
        self.assertIn("ref table Orders", content)
        self.assertIn("ref table Products", content)

    def test_write_model_tmdl_with_roles(self):
        model = {"culture": "en-US"}
        tables = [{"name": "Orders"}]
        roles = [{"name": "Admin"}]
        _write_model_tmdl(self.tmpdir, model, tables, roles=roles)
        with open(os.path.join(self.tmpdir, "model.tmdl"), "r") as f:
            content = f.read()
        self.assertIn("ref role Admin", content)

    def test_write_model_tmdl_with_perspectives(self):
        model = {"culture": "en-US", "perspectives": [{"name": "Full Model"}]}
        tables = [{"name": "Orders"}]
        _write_model_tmdl(self.tmpdir, model, tables)
        with open(os.path.join(self.tmpdir, "model.tmdl"), "r") as f:
            content = f.read()
        self.assertIn("ref perspective", content)

    def test_write_model_tmdl_with_culture_ref(self):
        model = {"culture": "fr-FR"}
        tables = [{"name": "Orders"}]
        _write_model_tmdl(self.tmpdir, model, tables)
        with open(os.path.join(self.tmpdir, "model.tmdl"), "r") as f:
            content = f.read()
        self.assertIn("ref culture", content)

    def test_write_relationships_tmdl(self):
        rels = [{
            "name": "rel_1",
            "fromTable": "Orders",
            "fromColumn": "ProductID",
            "toTable": "Products",
            "toColumn": "ProductID",
            "crossFilteringBehavior": "oneDirection",
            "cardinality": "manyToOne",
        }]
        _write_relationships_tmdl(self.tmpdir, rels)
        path = os.path.join(self.tmpdir, "relationships.tmdl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("relationship", content)
        self.assertIn("Orders", content)
        self.assertIn("Products", content)

    def test_write_table_tmdl(self):
        table = {
            "name": "Orders",
            "lineageTag": "test-tag",
            "columns": [{
                "name": "OrderID",
                "dataType": "int64",
                "sourceColumn": "OrderID",
                "lineageTag": "col-tag",
                "summarizeBy": "none",
            }],
            "measures": [{
                "name": "Total Sales",
                "lineageTag": "meas-tag",
                "expression": "SUM('Orders'[Amount])",
            }],
            "partitions": [{
                "source": {"type": "m", "expression": "let\n    Source = null\nin\n    Source"},
                "mode": "import",
            }],
            "hierarchies": [],
        }
        tables_dir = os.path.join(self.tmpdir, "tables")
        os.makedirs(tables_dir, exist_ok=True)
        _write_table_tmdl(tables_dir, table)
        path = os.path.join(tables_dir, "Orders.tmdl")
        self.assertTrue(os.path.exists(path))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("table Orders", content)
        self.assertIn("column OrderID", content)
        self.assertIn("measure 'Total Sales'", content)

    def test_write_tmdl_files_full(self):
        model_data = {
            "model": {
                "culture": "fr-FR",
                "compatibilityLevel": 1567,
                "tables": [{
                    "name": "Sales",
                    "lineageTag": "t1",
                    "columns": [{
                        "name": "Amount",
                        "dataType": "double",
                        "sourceColumn": "Amount",
                        "lineageTag": "c1",
                        "summarizeBy": "sum",
                    }],
                    "measures": [],
                    "partitions": [{"source": {"type": "m", "expression": "let\n    Source = null\nin\n    Source"}, "mode": "import"}],
                    "hierarchies": [],
                }],
                "relationships": [],
                "roles": [],
                "perspectives": [{"name": "Full Model", "tables": ["Sales"]}],
            }
        }
        def_dir = _write_tmdl_files(model_data, self.tmpdir)

        # Check all expected files exist
        self.assertTrue(os.path.exists(os.path.join(def_dir, "database.tmdl")))
        self.assertTrue(os.path.exists(os.path.join(def_dir, "model.tmdl")))
        self.assertTrue(os.path.exists(os.path.join(def_dir, "relationships.tmdl")))
        self.assertTrue(os.path.exists(os.path.join(def_dir, "expressions.tmdl")))
        self.assertTrue(os.path.exists(os.path.join(def_dir, "diagramLayout.json")))
        self.assertTrue(os.path.exists(os.path.join(def_dir, "perspectives.tmdl")))
        self.assertTrue(os.path.exists(os.path.join(def_dir, "tables", "Sales.tmdl")))

        # Check culture file
        culture_path = os.path.join(def_dir, "cultures", "fr-FR.tmdl")
        self.assertTrue(os.path.exists(culture_path))

        # Check diagramLayout is empty JSON
        with open(os.path.join(def_dir, "diagramLayout.json"), "r") as f:
            self.assertEqual(json.load(f), {})


# ═══════════════════════════════════════════════════════════════════════
# generate_tmdl Integration Test
# ═══════════════════════════════════════════════════════════════════════

class TestLocalSourceErrorRegressions(unittest.TestCase):
    """Synthetic file/navigation audits must surface errors without guessing sources."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = os.path.join(self.tmp.name, "Data")
        self.output = os.path.join(self.tmp.name, "Synthetic.SemanticModel")
        os.makedirs(self.data)
        os.makedirs(self.output)

    @staticmethod
    def _model(expression, name="Model"):
        return {"model": {"tables": [{
            "name": name, "partitions": [{
                "name": "Source", "source": {"type": "m", "expression": expression},
            }],
        }]}}

    @staticmethod
    def _excel_expression(filename, sheet):
        return (
            'let Source = Excel.Workbook(File.Contents(DataFolder & "\\\\'
            + filename.replace('"', '""') + '"), null, true), '
            'Selected = Source{[Item="' + sheet.replace('"', '""')
            + '",Kind="Sheet"]}[Data] in Selected'
        )

    def _xlsx(self, filename, sheets):
        import xml.etree.ElementTree as ET
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        workbook = ET.Element(f"{{{namespace}}}workbook")
        children = ET.SubElement(workbook, f"{{{namespace}}}sheets")
        for index, sheet in enumerate(sheets, 1):
            ET.SubElement(children, f"{{{namespace}}}sheet",
                          {"name": sheet, "sheetId": str(index)})
        with zipfile.ZipFile(os.path.join(self.data, filename), "w") as archive:
            archive.writestr("xl/workbook.xml", ET.tostring(workbook))

    def test_missing_file_reports_and_replaces_empty_fallback_with_explicit_error(self):
        model = self._model(
            'let Source = try Csv.Document(File.Contents(DataFolder & "\\\\missing.csv")) '
            'otherwise #table({"id"}, {}) in Source'
        )
        issues = _audit_local_partition_sources(model, self.output)
        self.assertEqual(issues, [{
            "table": "Model", "source": "missing.csv", "issue": "file_not_found",
        }])
        _fail_unavailable_local_sources(model, issues)
        expression = model["model"]["tables"][0]["partitions"][0]["source"]["expression"]
        self.assertTrue(expression.startswith('error Error.Record("Migration.SourceUnavailable",'))
        self.assertIn('Source = "missing.csv"', expression)
        self.assertIn('Reason = "file_not_found"', expression)
        self.assertNotIn("otherwise", expression)
        self.assertNotIn("#table(", expression)

    def test_exact_worksheet_is_required_without_substituting_first_sheet(self):
        self._xlsx("synthetic.xlsx", ["Other", "Exact"])
        model = self._model(self._excel_expression("synthetic.xlsx", "exact"))
        issues = _audit_local_partition_sources(model, self.output)
        self.assertEqual(issues, [{
            "table": "Model", "source": "synthetic.xlsx", "issue": "worksheet_not_found",
            "worksheet": "exact", "available_worksheets": ["Exact", "Other"],
        }])
        _fail_unavailable_local_sources(model, issues)
        expression = model["model"]["tables"][0]["partitions"][0]["source"]["expression"]
        self.assertIn('Worksheet = "exact"', expression)
        self.assertNotIn('Item="Other"', expression)

    def test_exact_quoted_apostrophe_and_unicode_sheet_is_not_rewritten(self):
        sheet = 'O\'Brien "réalisé"'
        self._xlsx("synthetic.xlsx", [sheet])
        expression = self._excel_expression("synthetic.xlsx", sheet)
        model = self._model(expression)
        issues = _audit_local_partition_sources(model, self.output)
        self.assertEqual(issues, [])
        _fail_unavailable_local_sources(model, issues)
        self.assertEqual(
            model["model"]["tables"][0]["partitions"][0]["source"]["expression"], expression
        )

    def test_invalid_xlsx_surfaces_concrete_issue_instead_of_empty_success(self):
        for content in (b"not a ZIP", b""):
            with self.subTest(content=content):
                with open(os.path.join(self.data, "broken.xlsx"), "wb") as stream:
                    stream.write(content)
                model = self._model(self._excel_expression("broken.xlsx", "Exact"))
                issues = _audit_local_partition_sources(model, self.output)
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0]["issue"], "invalid_xlsx")
                self.assertEqual(issues[0]["source"], "broken.xlsx")
                self.assertTrue(issues[0]["detail"])

    def test_valid_header_only_csv_remains_valid_empty_source(self):
        with open(os.path.join(self.data, "empty.csv"), "w", encoding="utf-8") as stream:
            stream.write("id,réalisé\n")
        expression = 'Csv.Document(File.Contents(DataFolder & "\\\\empty.csv"))'
        model = self._model(expression)
        issues = _audit_local_partition_sources(model, self.output)
        self.assertEqual(issues, [])
        _fail_unavailable_local_sources(model, issues)
        self.assertEqual(
            model["model"]["tables"][0]["partitions"][0]["source"]["expression"], expression
        )

    def test_qualified_relation_without_connection_has_renamed_provenance_issue(self):
        model = self._model("#table({}, {})", name="Renamed")
        model["_table_rename_map"] = {("source-a", "Original"): "Renamed"}
        issues = _audit_local_partition_sources(model, self.output, [{
            "name": "source-a", "connection": {
                "type": "Excel", "details": {"filename": "foreign.xlsx"},
            },
            "tables": [{"name": "Original", "source_table": "[namespace].[Exact]"}],
        }])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["table"], "Renamed")
        self.assertEqual(issues[0]["issue"], "source_provenance_unverified")
        self.assertEqual(issues[0]["source"], "[namespace].[Exact]")

    def test_table_connection_binding_is_not_confused_with_datasource_default(self):
        model = self._model("#table({}, {})")
        issues = _audit_local_partition_sources(model, self.output, [{
            "name": "source-a",
            "connection_map": {"local": {
                "type": "Excel", "details": {"filename": "synthetic.xlsx"},
            }},
            "tables": [{
                "name": "Model", "source_table": "[namespace].[Exact]",
                "connection": "local",
            }],
        }])
        self.assertEqual(issues, [])

    def test_csv_relation_mismatch_is_reported_but_matching_hash_relation_is_not(self):
        for filename, expected in (("foreign.csv", "source_relation_mismatch"),
                                   ("Exact.csv", None)):
            with self.subTest(filename=filename):
                issues = _audit_local_partition_sources(
                    self._model("#table({}, {})"), self.output, [{
                        "name": "source-a", "tables": [{
                            "name": "Model", "source_table": "[Exact#csv]",
                            "connection_details": {
                                "type": "CSV", "details": {"filename": filename},
                            },
                        }],
                    }],
                )
                self.assertEqual([issue["issue"] for issue in issues],
                                 [expected] if expected else [])

    def test_multi_source_queries_do_not_assign_sheet_navigation_to_wrong_file(self):
        self._xlsx("one.xlsx", ["One"])
        self._xlsx("two.xlsx", ["Two"])
        expression = (
            self._excel_expression("one.xlsx", "Unknown") + " & "
            'Excel.Workbook(File.Contents(DataFolder & "\\\\two.xlsx"), null, true)'
        )
        self.assertEqual(
            _audit_local_partition_sources(self._model(expression), self.output), []
        )

    def test_refresh_error_escapes_quotes_and_preserves_other_tables_and_calculated_partitions(self):
        original = "#table({}, {})"
        model = self._model(original, name='Réalisé "net"')
        tables = model["model"]["tables"]
        tables[0]["partitions"].append({
            "name": "Calculated", "source": {"type": "calculated", "expression": "{1}"},
        })
        tables.append({"name": "Valid", "partitions": [{
            "name": "Source", "source": {"type": "m", "expression": original},
        }]})
        _fail_unavailable_local_sources(model, [{
            "table": 'Réalisé "net"', "source": 'a"b.xlsx',
            "issue": "worksheet_not_found", "worksheet": 'O\'Brien "net"',
        }])
        expression = tables[0]["partitions"][0]["source"]["expression"]
        self.assertIn('Table = "Réalisé ""net"""', expression)
        self.assertIn('Source = "a""b.xlsx"', expression)
        self.assertIn('Worksheet = "O\'Brien ""net"""', expression)
        self.assertEqual(tables[0]["partitions"][1]["source"]["expression"], "{1}")
        self.assertEqual(tables[1]["partitions"][0]["source"]["expression"], original)

    def test_public_native_import_defers_worksheet_inspection_until_refresh(self):
        from contextlib import redirect_stdout
        connection = {"type": "Excel", "details": {"filename": "synthetic.xlsx"}}
        self._xlsx("synthetic.xlsx", ["Other"])
        datasources = [{
            "name": "source-a", "connection": connection,
            "connection_map": {"local": connection},
            "tables": [{
                "name": "Model", "source_table": "Exact$", "connection": "local",
                "connection_details": connection,
                "columns": [{"name": "id", "datatype": "integer"}],
            }],
            "calculations": [], "columns": [], "relationships": [],
        }]
        with forbid_native_payload_io([os.path.join(self.data, "synthetic.xlsx")]), \
                redirect_stdout(io.StringIO()):
            stats = generate_tmdl(
                datasources, "Synthetic", None, self.output, parameterize=False,
            )
        self.assertEqual(stats["source_issues"], [])
        with open(os.path.join(self.output, "definition", "tables", "Model.tmdl"),
                  encoding="utf-8") as stream:
            written = stream.read()
        self.assertIn('File.Contents("synthetic.xlsx")', written)
        self.assertIn('Item="Exact",Kind="Sheet"', written)
        self.assertNotIn("DataFolder", written)
        self.assertNotIn("Migration.SourceUnavailable", written)
        self.assertNotIn('Item="Other"', written)
        self.assertNotIn("otherwise #table", written)


class TestNativeImportMetadataOnly(unittest.TestCase):
    """Native Import emits bindings/schema/M without materializing native rows."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="native_import_")
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.output = os.path.join(self.root, "Report.SemanticModel")

    @staticmethod
    def _columns():
        return [{"name": "ID", "datatype": "integer"},
                {"name": "Amount", "datatype": "real"}]

    def _generate(self, datasources, mode="import"):
        from contextlib import redirect_stdout

        with redirect_stdout(io.StringIO()):
            return generate_tmdl(
                datasources, "Report", None, self.output,
                model_mode=mode, parameterize=False,
            )

    def _content(self, table):
        path = os.path.join(self.output, "definition", "tables", table + ".tmdl")
        self.assertTrue(os.path.isfile(path), path)
        with open(path, encoding="utf-8") as stream:
            content = stream.read()
        self.assertEqual(re.findall(r"^\s*mode:\s+(\w+)\s*$", content, re.M), ["import"])
        self.assertIn("sourceColumn: ID", content)
        self.assertIn("sourceColumn: Amount", content)
        return content

    def test_sql_import_writes_physical_connection_schema_and_m_without_querying_rows(self):
        connection = {"type": "SQL Server", "details": {
            "server": "synthetic.invalid", "database": "SyntheticDB",
            "schema": "not_the_physical_schema",
        }}
        datasource = {
            "name": "NativeSQL",
            "connection": connection,
            "tables": [{
                "name": "FriendlyCaption", "source_table": "[reporting].[Physical Orders]",
                "connection_details": connection, "columns": self._columns(),
            }],
            "calculations": [], "relationships": [],
        }
        with patch("socket.create_connection", side_effect=AssertionError(
                "SQL rows must not be retrieved during migration")) as network, \
                patch("socket.socket.connect", side_effect=AssertionError(
                    "Live source access belongs to refresh")) as socket_connect, \
                patch("sqlite3.connect", side_effect=AssertionError(
                    "Native SQL must not be staged through SQLite")) as sqlite, \
                patch("tableau_export.hyper_reader.read_hyper") as hyper_reader:
            stats = self._generate([datasource])
        network.assert_not_called()
        socket_connect.assert_not_called()
        sqlite.assert_not_called()
        hyper_reader.assert_not_called()
        content = self._content("FriendlyCaption")
        self.assertIn('Sql.Database("synthetic.invalid", "SyntheticDB")', content)
        self.assertIn('[Schema="reporting", Item="Physical Orders"]', content)
        self.assertNotIn('Item="FriendlyCaption"', content)
        self.assertIn("dataType: int64", content)
        self.assertIn("dataType: double", content)
        self.assertNotIn("File.Contents", content)
        self.assertNotIn("otherwise", content)
        self.assertEqual(stats["source_issues"], [])
        self.assertFalse(os.path.exists(os.path.join(self.root, "Data")))

    def test_file_import_preserves_same_basename_per_table_and_never_opens_payloads(self):
        connections = {}
        tables = []
        expected_paths = {}
        payload_paths = []
        for name, connector, folder, filename, relation in (
            ("WestFacts", "CSV", "west", "facts.csv", "[facts#csv]"),
            ("EastFacts", "CSV", "east", "facts.csv", "[facts#csv]"),
            ("ExcelFacts", "Excel", "books", "facts.xlsx", "[Exact$]"),
            ("JsonFacts", "JSON", "json", "facts.json", "facts.json"),
        ):
            directory = os.path.join(self.root, "native", folder)
            os.makedirs(directory)
            path = os.path.join(directory, filename)
            with open(path, "wb") as stream:
                stream.write(b"synthetic body must stay at source; not a parsed dataset")
            payload_paths.append(path)
            expected_paths[name] = path
            connection = {
                "type": connector, "details": {"directory": directory, "filename": filename},
            }
            connections[name] = connection
            tables.append({
                "name": name, "source_table": relation, "connection": name,
                "connection_details": connection, "columns": self._columns(),
            })
        datasource = {
            "name": "NativeFiles",
            "connection": {"type": "CSV", "details": {"filename": "decoy.csv"}},
            "connection_map": connections, "tables": tables,
            "calculations": [], "relationships": [],
        }
        for mode in ("auto", "import"):
            with self.subTest(mode=mode), forbid_native_payload_io(payload_paths):
                stats = self._generate([datasource], mode=mode)
            self.assertEqual(stats["source_issues"], [])
            for name, expected in expected_paths.items():
                content = self._content(name)
                paths = re.findall(r'File\.Contents\("((?:[^"]|"")*)"\)', content)
                self.assertEqual(len(paths), 1, content)
                self.assertEqual(
                    ntpath.normpath(paths[0].replace('""', '"')),
                    ntpath.normpath(expected),
                )
                self.assertNotIn("DataFolder", content)
                self.assertNotIn("decoy.csv", content)
                self.assertNotIn("Migration.SourceUnavailable", content)
                self.assertNotIn("otherwise", content)
                if name == "ExcelFacts":
                    self.assertIn('Item="Exact",Kind="Sheet"', content)
            self.assertFalse(os.path.exists(os.path.join(self.root, "Data")))

    def test_native_relative_binding_is_not_replaced_by_output_datafolder(self):
        datasource = {
            "name": "RelativeFile",
            "connection": {"type": "CSV", "details": {
                "directory": r"..\unavailable-native", "filename": "facts.csv",
            }},
            "tables": [{
                "name": "RelativeFacts", "source_table": "[facts#csv]",
                "columns": self._columns(),
            }],
            "calculations": [], "relationships": [],
        }
        data = os.path.join(self.root, "Data")
        os.makedirs(data)
        decoy = os.path.join(data, "facts.csv")
        with open(decoy, "w", encoding="utf-8") as stream:
            stream.write("ID,Amount\n999,999\n")
        with forbid_native_payload_io([decoy]):
            self._generate([datasource])
        content = self._content("RelativeFacts")
        self.assertIn(r'File.Contents("..\unavailable-native\facts.csv")', content)
        self.assertNotIn("DataFolder", content)
        self.assertNotIn("999", content)
        self.assertNotIn("Migration.SourceUnavailable", content)

    def test_deferred_payload_access_does_not_hide_invalid_binding_metadata(self):
        for connector, details, relation, expected_issue in (
            ("CSV", {"filename": "foreign.csv"}, "[Exact#csv]", "source_relation_mismatch"),
            ("Excel", {"filename": "exact.xlsx"}, "[namespace].[Exact$]",
             "source_provenance_unverified"),
        ):
            with self.subTest(connector=connector):
                datasource = {
                    "name": "InvalidBinding",
                    "connection": {"type": connector, "details": details},
                    "tables": [{
                        "name": "InvalidFacts", "source_table": relation,
                        "columns": self._columns(),
                    }],
                    "calculations": [], "relationships": [],
                }
                stats = self._generate([datasource])
                self.assertEqual(
                    [issue["issue"] for issue in stats["source_issues"]], [expected_issue],
                )
                content = self._content("InvalidFacts")
                self.assertIn("Migration.SourceUnavailable", content)
                self.assertIn(expected_issue, content)
                self.assertNotIn("otherwise", content)


class TestSourceErrorsRemainVisible(unittest.TestCase):
    """Generated M must propagate connector/navigation errors at refresh."""

    connectors = (
        ('SQL Server', 'Sql.Database'),
        ('Oracle', 'Oracle.Database'),
        ('PostgreSQL', 'PostgreSQL.Database'),
        ('Snowflake', 'Snowflake.Databases'),
        ('OData', 'OData.Feed'),
        ('Web', 'Web.Contents'),
        ('Excel', 'Excel.Workbook'),
        ('CSV', 'Csv.Document'),
    )

    @staticmethod
    def _connection(connector):
        return {'type': connector, 'details': {
            'server': 'unavailable.invalid', 'database': 'missing_database',
            'service': 'missing_service', 'warehouse': 'missing_warehouse',
            'url': 'https://example.invalid/data', 'filename': 'missing.csv',
        }}

    @staticmethod
    def _table():
        return {'name': 'Orders', 'source_table': 'MissingOrders', 'columns': [
            {'name': 'ID', 'datatype': 'integer'},
        ]}

    def test_build_table_never_wraps_generated_connector_or_navigation_steps(self):
        from powerbi_import.errors import ConfigurationError
        from powerbi_import.tmdl_generator import _build_table, generate_power_query_m
        from tableau_export.m_query_builder import wrap_source_with_try_otherwise
        for connector, native_function in self.connectors:
            for mode in ('import', 'directquery', 'composite'):
                with self.subTest(connector=connector, mode=mode):
                    connection = self._connection(connector)
                    table = self._table()
                    raw = generate_power_query_m(
                        connection, table,
                        use_original_file_paths=(
                            connector in ('Excel', 'CSV') and mode != 'directquery'
                        ),
                    )
                    self.assertEqual(wrap_source_with_try_otherwise(raw), raw)
                    self.assertEqual(wrap_source_with_try_otherwise(
                        raw, ['Ignored "fallback" column']), raw)
                    if mode == 'directquery' and connector in ('Excel', 'CSV'):
                        with self.assertRaises(ConfigurationError) as raised:
                            _build_table(table, connection, [], [], model_mode=mode)
                        self.assertIn('DirectQuery', str(raised.exception))
                        self.assertIn(f'file-based {connector}', str(raised.exception))
                        self.assertIn('Import', str(raised.exception))
                        self.assertEqual(raised.exception.source, connector)
                        self.assertEqual(raised.exception.item, table['name'])
                        continue
                    result = _build_table(table, connection, [], [], model_mode=mode)
                    expr = result['partitions'][0]['source']['expression']
                    self.assertIn(native_function, expr)
                    self.assertNotIn('otherwise', expr)
                    self.assertNotIn('#table(', expr)
                    self.assertEqual(expr, raw)

    def test_prep_override_preserves_authored_source_try_and_value_try(self):
        from powerbi_import.tmdl_generator import _build_table, _self_heal_model
        for expr in (
            'let\n    Source = Sql.Database("s", "d")\nin Source',
            'let\n    Source = try Oracle.Database("s") otherwise #table({"ID"}, {})\nin Source',
            'let\n    Source = Sql.Database("s", "d"),\n'
            '    Parsed = Table.AddColumn(Source, "Value", '
            'each try Number.FromText([Text]) otherwise null)\nin Parsed',
            'let\n    Source = error Error.Record("Connection.Error", "offline", [])\nin Source',
        ):
            with self.subTest(expr=expr):
                table = _build_table(self._table(), self._connection('SQL Server'),
                                     [], [], m_query_override=expr)
                _self_heal_model({'model': {'tables': [table], 'relationships': []}})
                self.assertEqual(table['partitions'][0]['source']['expression'], expr)

    def test_public_generation_persists_native_sources_without_empty_fallback(self):
        from contextlib import redirect_stdout
        for connector, native_function in self.connectors[:6]:
            with self.subTest(connector=connector), tempfile.TemporaryDirectory() as output:
                datasources = [{
                    'name': 'SourceDS', 'connection': self._connection(connector),
                    'tables': [self._table()], 'calculations': [],
                    'columns': [], 'relationships': [],
                }]
                with redirect_stdout(io.StringIO()):
                    generate_tmdl(datasources, 'Synthetic', None, output)
                with open(os.path.join(output, 'definition', 'tables', 'Orders.tmdl'),
                          encoding='utf-8') as stream:
                    written = stream.read()
                self.assertIn(native_function, written)
                self.assertIn('unavailable.invalid' if connector != 'Web'
                              else 'example.invalid', written)
                self.assertNotIn('otherwise', written)
                self.assertNotIn('#table(', written)


class TestGenerateTmdl(unittest.TestCase):
    """Integration test for generate_tmdl entry point."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _generate(self, *args, **kwargs):
        """Wrapper that redirects stdout to avoid cp1252 Unicode errors."""
        old_stdout = sys.stdout
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        try:
            return generate_tmdl(*args, **kwargs)
        finally:
            sys.stdout = old_stdout

    def test_generates_valid_output(self):
        datasources = [{
            "name": "TestDS",
            "caption": "Test",
            "connection": {"type": "CSV", "details": {"filename": "test.csv"}},
            "tables": [{
                "name": "Orders",
                "type": "table",
                "columns": [
                    {"name": "OrderID", "datatype": "integer"},
                    {"name": "Amount", "datatype": "real"},
                ],
            }],
            "calculations": [],
            "columns": [],
            "relationships": [],
            "connection_map": {},
        }]
        stats = self._generate(datasources, "TestReport", None, self.tmpdir)
        self.assertIsInstance(stats, dict)
        self.assertIn("tables", stats)
        self.assertGreaterEqual(stats["tables"], 1)

    def test_stats_include_all_counts(self):
        datasources = [{
            "name": "DS",
            "caption": "DS",
            "connection": {"type": "CSV", "details": {"filename": "data.csv"}},
            "tables": [{
                "name": "T1",
                "type": "table",
                "columns": [
                    {"name": "ID", "datatype": "integer"},
                    {"name": "Val", "datatype": "real"},
                ],
            }],
            "calculations": [{
                "name": "[Calculation_1]",
                "caption": "TotalVal",
                "formula": "SUM([Val])",
                "role": "measure",
                "datatype": "real",
            }],
            "columns": [],
            "relationships": [],
            "connection_map": {},
        }]
        stats = self._generate(datasources, "Test", None, self.tmpdir)
        for key in ["tables", "columns", "measures", "relationships"]:
            self.assertIn(key, stats)


class TestRewriteBareMeasureReferences(unittest.TestCase):
    """Regression: renaming a measure (collision self-heal) must update every
    other DAX expression that bare-references the old name, or Power BI
    Desktop fails to load with "value cannot be determined" (issue seen in
    the NBA player stats sample: a parameter measure named 'Rebounds' renamed
    to 'Rebounds (Measure)' left a SWITCH([Rebounds], ...) reference dangling).
    """

    def _model(self):
        return {"model": {"tables": [
            {"name": "Params", "measures": [
                {"name": "Rebounds", "expression": "SELECTEDVALUE('Params'[Value], 1)"},
            ], "columns": []},
            {"name": "Sheet1", "measures": [
                {"name": "p. Rebounds",
                 "expression": "SWITCH([Rebounds], 1, AVERAGE('Sheet1'[DEF]))"},
            ], "columns": []},
        ]}}

    def test_rewrites_bare_reference_in_other_measure(self):
        model = self._model()
        _rewrite_bare_measure_references(model, "Rebounds", "Rebounds (Measure)")
        other = model["model"]["tables"][1]["measures"][0]
        self.assertEqual(
            other["expression"],
            "SWITCH([Rebounds (Measure)], 1, AVERAGE('Sheet1'[DEF]))")

    def test_leaves_qualified_column_reference_untouched(self):
        model = {"model": {"tables": [
            {"name": "T1", "measures": [
                {"name": "Rebounds", "expression": "1"},
            ], "columns": []},
            {"name": "T2", "measures": [
                {"name": "M", "expression": "SUM('T2'[Rebounds])"},
            ], "columns": []},
        ]}}
        _rewrite_bare_measure_references(model, "Rebounds", "Rebounds (Measure)")
        self.assertEqual(
            model["model"]["tables"][1]["measures"][0]["expression"],
            "SUM('T2'[Rebounds])")

    def test_noop_when_names_match(self):
        model = self._model()
        before = json.loads(json.dumps(model))
        _rewrite_bare_measure_references(model, "Rebounds", "Rebounds")
        self.assertEqual(model, before)


class TestColumnDeduplication(unittest.TestCase):
    """Regression: duplicate columns/measures must be deduplicated.

    PBI Desktop rejects TMDL when two columns declare the same 'expression'
    property on the same table (e.g. 'Filtre période par défaut').
    """

    def test_calc_column_replaces_physical_column_same_name(self):
        """If a physical col and a calc col share the same caption, only one should remain."""
        from powerbi_import.tmdl_generator import _build_table

        columns = [
            {'name': 'Filtre période par défaut', 'datatype': 'string'},
            {'name': 'Region', 'datatype': 'string'},
        ]
        calculations = [{
            'name': 'Filtre période par défaut',
            'caption': 'Filtre période par défaut',
            'formula': 'IF([Status]="Active", "Oui", "Non")',
            'role': 'dimension',
            'datatype': 'string',
        }]
        table = {'name': 'sqlproxy', 'columns': columns}
        connection = {'type': 'sqlserver', 'server': 'srv', 'database': 'db'}
        dax_context = {
            'calc_map': {}, 'param_map': {}, 'column_table_map': {},
            'measure_names': set(), 'param_values': {},
        }
        result = _build_table(
            table, connection, calculations, [],
            dax_context=dax_context, col_metadata_map={},
        )
        col_names = [c['name'] for c in result['columns']]
        # Only one instance of the column
        self.assertEqual(col_names.count('Filtre période par défaut'), 1)

    def test_duplicate_calc_columns_deduplicated(self):
        """Two calculations with same caption produce only one column."""
        from powerbi_import.tmdl_generator import _build_table

        columns = [{'name': 'ID', 'datatype': 'integer'}]
        calculations = [
            {
                'name': 'calc.MyCalc',
                'caption': 'MyCalc',
                'formula': '[A] + [B]',
                'role': 'dimension',
                'datatype': 'integer',
            },
            {
                'name': 'other.MyCalc',
                'caption': 'MyCalc',
                'formula': '[A] + [B] + [C]',
                'role': 'dimension',
                'datatype': 'integer',
            },
        ]
        table = {'name': 'TestTable', 'columns': columns}
        connection = {'type': 'sqlserver', 'server': 'srv', 'database': 'db'}
        dax_context = {
            'calc_map': {}, 'param_map': {}, 'column_table_map': {},
            'measure_names': set(), 'param_values': {},
        }
        result = _build_table(
            table, connection, calculations, [],
            dax_context=dax_context, col_metadata_map={},
        )
        col_names = [c['name'] for c in result['columns']]
        self.assertEqual(col_names.count('MyCalc'), 1)

    def test_duplicate_measures_deduplicated(self):
        """Two calculations resolving to the same measure name: only one kept."""
        from powerbi_import.tmdl_generator import _build_table

        columns = [{'name': 'Amount', 'datatype': 'real'}]
        calculations = [
            {
                'name': 'calc.Total',
                'caption': 'Total',
                'formula': 'SUM([Amount])',
                'role': 'measure',
                'datatype': 'real',
            },
            {
                'name': 'other.Total',
                'caption': 'Total',
                'formula': 'SUM([Amount])',
                'role': 'measure',
                'datatype': 'real',
            },
        ]
        table = {'name': 'TestTable', 'columns': columns}
        connection = {'type': 'sqlserver', 'server': 'srv', 'database': 'db'}
        dax_context = {
            'calc_map': {}, 'param_map': {}, 'column_table_map': {},
            'measure_names': set(), 'param_values': {},
        }
        result = _build_table(
            table, connection, calculations, [],
            dax_context=dax_context, col_metadata_map={},
        )
        measure_names = [m['name'] for m in result['measures']]
        self.assertEqual(measure_names.count('Total'), 1)

    def test_tmdl_writer_dedup_columns(self):
        """_write_table_tmdl deduplicates columns by name (defense in depth)."""
        import tempfile

        from powerbi_import.tmdl_generator import _write_table_tmdl

        table = {
            'name': 'sqlproxy',
            'columns': [
                {'name': 'Filtre', 'dataType': 'string',
                 'sourceColumn': 'Filtre'},
                {'name': 'Filtre', 'dataType': 'string',
                 'expression': 'IF(TRUE, "A", "B")', 'isCalculated': True},
            ],
            'measures': [],
            'partitions': [{'name': 'P', 'mode': 'import',
                            'source': {'type': 'm',
                                       'expression': 'let x=1 in x'}}],
        }
        with tempfile.TemporaryDirectory() as td:
            _write_table_tmdl(td, table)
            import os
            with open(os.path.join(td, 'sqlproxy.tmdl'), 'r',
                       encoding='utf-8') as f:
                content = f.read()
        # Column name appears exactly once as a TMDL column declaration
        import re
        col_decls = re.findall(r'^\tcolumn .*Filtre', content, re.MULTILINE)
        self.assertEqual(len(col_decls), 1, f'Expected 1 column, got: {col_decls}')

    def test_tmdl_writer_dedup_measures(self):
        """_write_table_tmdl deduplicates measures by name."""
        import tempfile

        from powerbi_import.tmdl_generator import _write_table_tmdl

        table = {
            'name': 'Facts',
            'columns': [{'name': 'Val', 'dataType': 'int64',
                          'sourceColumn': 'Val'}],
            'measures': [
                {'name': 'Total', 'expression': 'SUM([Val])'},
                {'name': 'Total', 'expression': 'SUM([Val])'},
            ],
            'partitions': [{'name': 'P', 'mode': 'import',
                            'source': {'type': 'm',
                                       'expression': 'let x=1 in x'}}],
        }
        with tempfile.TemporaryDirectory() as td:
            _write_table_tmdl(td, table)
            import os
            with open(os.path.join(td, 'Facts.tmdl'), 'r',
                       encoding='utf-8') as f:
                content = f.read()
        import re
        measure_decls = re.findall(r'^\tmeasure .*Total', content, re.MULTILINE)
        self.assertEqual(len(measure_decls), 1,
                         f'Expected 1 measure, got: {measure_decls}')


class TestWorksheetSupportedCalendarRegressions(unittest.TestCase):
    """Calendar selection follows worksheet date axes, not column-name ties."""

    def _build(self, axis='Order Date', **options):
        from contextlib import redirect_stdout
        datasource = {
            'name': 'SalesDS',
            'connection': {'type': 'CSV', 'details': {'filename': 'orders.csv'}},
            'tables': [{
                'name': 'Orders',
                'columns': [
                    {'name': 'Ship Date', 'datatype': 'date'},
                    {'name': 'Order Date', 'datatype': 'date'},
                    {'name': 'Quantity', 'datatype': 'integer'},
                ],
            }],
            'calculations': [], 'relationships': [],
        }
        worksheet = {'name': 'Q', 'original_mark_class': 'Automatic',
                     'fields': [
                         {'name': axis, 'datasource': 'SalesDS', 'shelf': 'columns',
                          'date_part': 'mn', 'derivation': 'Month',
                          'is_discrete': True, 'is_continuous': False},
                         {'name': 'Quantity', 'datasource': 'SalesDS',
                          'shelf': 'rows', 'aggregation': 'sum'},
                     ]}
        with redirect_stdout(io.StringIO()):
            return _build_semantic_model(
                [datasource], extra_objects={'worksheets': [worksheet]}, **options)

    def _calendar_m(self, model):
        calendars = [t for t in model['model']['tables'] if t['name'] == 'Calendar']
        self.assertEqual(len(calendars), 1)
        return calendars[0]['partitions'][0]['source']['expression']

    def test_order_date_axis_wins_over_ship_date_name_tie(self):
        model = self._build()
        active = [r for r in model['model']['relationships']
                  if r['fromTable'] == 'Orders' and r['toTable'] == 'Calendar'
                  and r.get('isActive', True)]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['fromColumn'], 'Order Date')
        self.assertEqual(active[0]['toColumn'], 'Date')

    def test_ship_date_axis_is_honored_not_hardcoded_order_date(self):
        model = self._build(axis='Ship Date')
        active = [r for r in model['model']['relationships']
                  if r['fromTable'] == 'Orders' and r['toTable'] == 'Calendar'
                  and r.get('isActive', True)]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['fromColumn'], 'Ship Date')

    def test_default_calendar_bounds_reference_selected_source_date(self):
        expression = self._calendar_m(self._build())
        # No sample is supplied: covering unknown source years requires runtime
        # source-date evidence rather than a fixed 2020–2030-only calendar.
        self.assertIn('Orders', expression)
        self.assertIn('Order Date', expression)
        self.assertNotIn('StartDate = #date(2020, 1, 1)', expression)
        self.assertNotIn('EndDate = #date(2030, 12, 31)', expression)

    def test_explicit_calendar_bounds_remain_authoritative(self):
        expression = self._calendar_m(
            self._build(calendar_start=2000, calendar_end=2040))
        self.assertIn('StartDate = #date(2000, 1, 1)', expression)
        self.assertIn('EndDate = #date(2040, 12, 31)', expression)

    def test_explicit_start_preserved_while_end_uses_source(self):
        expression = self._calendar_m(self._build(calendar_start=2000))
        self.assertIn('StartDate = #date(2000, 1, 1)', expression)
        self.assertIn('Orders', expression)
        self.assertIn('Order Date', expression)
        self.assertNotIn('EndDate = #date(2030, 12, 31)', expression)

    def test_explicit_end_preserved_while_start_uses_source(self):
        expression = self._calendar_m(self._build(calendar_end=2040))
        self.assertIn('EndDate = #date(2040, 12, 31)', expression)
        self.assertIn('Orders', expression)
        self.assertIn('Order Date', expression)
        self.assertNotIn('StartDate = #date(2020, 1, 1)', expression)


if __name__ == '__main__':
    unittest.main(verbosity=2)
