"""
TMDL (Tabular Model Definition Language) Generator

Converts extracted Tableau data directly into TMDL files
for the Power BI SemanticModel.

Handles:
- Physical tables with M query partitions
- DAX measures and calculated columns
- Relationships (manyToOne, manyToMany)
- Hierarchies, sets, groups, bins
- Parameter tables (What-If)
- Date table with time intelligence
- Geographic data categories
- RLS roles from Tableau user filters

Generated structure:
  definition/
    database.tmdl
    model.tmdl
    relationships.tmdl
    expressions.tmdl
    roles.tmdl (if RLS)
    tables/
      {TableName}.tmdl
"""

import sys
import os
import re
import uuid
import json
import logging
import shutil
import time

logger = logging.getLogger(__name__)

# Add path to import from tableau_export
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from datasource_extractor import (
    generate_power_query_m,
    convert_tableau_formula_to_dax,
    map_tableau_to_powerbi_type,
    sanitize_param_brackets,
)
from m_query_builder import (
    inject_m_steps,
    m_transform_rename,
    m_transform_remove_columns,
    m_transform_filter_values,
    m_transform_filter_nulls,
    m_transform_add_column,
    wrap_source_with_try_otherwise,
    generate_m_from_hyper,
)




# DAX -> Power Query M conversion lives in tmdl_m_conversion (owned by @wiring).
# Re-exported here so existing tmdl_generator callers keep working.
from powerbi_import.tmdl_m_conversion import (  # noqa: F401
    _DAX_TO_M_TYPE,
    _DATE_DATATYPES,
    _RE_COL_SUBTRACTION,
    _dax_to_m_expression,
    _extract_function_body,
    _inject_m_steps_into_partition,
    _M_SPECIAL_CHARS,
    _quote_m_identifiers,
    _split_dax_args,
    _split_top_level_binop,
    _strip_m_inline_comments,
    _build_m_transform_steps,
    _fix_m_if_else_balance,
    _wrap_date_subtraction_in_duration_days,
)


# DAX post-processing lives in tmdl_dax_postprocess (owned by @dax);
# re-exported here so existing imports keep working.
from powerbi_import.tmdl_dax_postprocess import (  # noqa: E402,F401
    _BARE_COL_REF_RE,
    _fix_related_for_many_to_many,
    _promote_measure_dependent_calc_columns,
    _replace_related_in_aggx_context,
    _replace_related_with_lookupvalue,
    _unwrap_aggregations_of_measures,
    _validate_measures_after_rewrites,
    _wrap_bare_cross_table_refs,
    _wrap_bare_ref_expression,
    resolve_table_for_column,
    resolve_table_for_formula,
)


# Pre-write TMDL self-healing lives in tmdl_self_heal (owned by @healing).
# Re-exported here so existing tmdl_generator callers keep working.
from powerbi_import.tmdl_self_heal import (  # noqa: F401
    _categorize_m_issue,
    _self_heal_model,
    _validate_m_partitions,
)


# ════════════════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINT
# ════════════════════════════════════════════════════════════════════

def _apply_time_intelligence(model):
    """Inject YTD/PY/YoY% measures for aggregation measures.

    The date reference is resolved from the model's own date table: a source
    that ships its own date dimension suppresses the generated Calendar, so a
    hardcoded ``'Calendar'[Date]`` would point at a column that does not exist.
    """
    from powerbi_import.dax_optimizer import generate_time_intelligence_measures

    tables = model.get('model', {}).get('tables', [])
    date_reference = ''
    for table in tables:
        if not _is_date_table(table):
            continue
        for column in table.get('columns', []):
            if (column.get('dataType') == 'DateTime'
                    or column.get('dataCategory') == 'DateTime'):
                safe_table = table.get('name', '').replace("'", "''")
                date_reference = f"'{safe_table}'[{column.get('name')}]"
                break
        if date_reference:
            break

    if not date_reference:
        print("  \u2139 Time intelligence skipped: the model has no date table")
        return 0

    taken = {m.get('name') for t in tables for m in t.get('measures', [])}
    taken |= {c.get('name') for t in tables for c in t.get('columns', [])}

    added = 0
    for table in tables:
        if _is_date_table(table):
            continue
        for measure in list(table.get('measures', [])):
            group = generate_time_intelligence_measures(
                [measure], date_column=date_reference)
            # All-or-nothing: YoY% refers to PY, so a partial group would dangle.
            if not group or any(ti['name'] in taken for ti in group):
                continue
            for time_measure in group:
                table.setdefault('measures', []).append(time_measure)
                taken.add(time_measure['name'])
            added += len(group)

    if added:
        print(f"  \u23f1 Time intelligence added {added} measure(s) "
              f"using {date_reference}")
    return added


def _apply_dax_optimization(model):
    """Rewrite generated measures with the DAX optimizer.

    The direct Tableau conversion is kept as an annotation, so a reviewer can
    always recover what the measure looked like before optimization.
    """
    from powerbi_import.dax_optimizer import optimize_dax as _optimize_expression

    rewritten = 0
    for table in model.get('model', {}).get('tables', []):
        for measure in table.get('measures', []):
            expression = measure.get('expression')
            if not isinstance(expression, str) or not expression.strip():
                continue
            optimized, applied = _optimize_expression(expression)
            if not applied or optimized == expression:
                continue
            measure['expression'] = optimized
            measure.setdefault('annotations', []).append({
                'name': 'MigrationNote',
                'value': (f"DAX optimized ({', '.join(applied)}); "
                          f"original: {expression}"),
            })
            rewritten += 1

    if rewritten:
        print(f"  \u26a1 DAX optimizer rewrote {rewritten} measure(s)")
    return rewritten


def generate_tmdl(datasources, report_name, extra_objects, output_dir,
                  calendar_start=None, calendar_end=None, culture=None,
                  model_mode='import', languages=None,
                  composite_threshold=None, agg_tables='none',
                  incremental_refresh=False, incremental_refresh_months=12,
                  parameterize=True, optimize_dax=False, time_intelligence='none'):
    """
    Main entry point: directly convert extracted Tableau data to TMDL files.

    Args:
        datasources: List of datasources with connections, tables, calculations
        report_name: Name of the report
        extra_objects: Dict with hierarchies, sets, groups, bins, aliases,
                       parameters, user_filters, _datasources
        output_dir: Path to the SemanticModel folder
        calendar_start: Start year for Calendar table (default: 2020)
        calendar_end: End year for Calendar table (default: 2030)
        culture: Override culture/locale (default: en-US)
        model_mode: 'import', 'directquery', or 'composite'
                    Controls partition mode for all tables
        languages: Comma-separated additional locales (e.g. 'fr-FR,de-DE')
        composite_threshold: Column count threshold for composite mode.
                    Tables with more columns than this → directQuery, fewer → import.
                    Default: 10 columns.
        agg_tables: 'auto' to generate Import-mode aggregation tables for
                    directQuery fact tables, 'none' to skip (default).
        incremental_refresh: If True, detect and configure incremental refresh
                    policies on eligible tables (default: False).
        incremental_refresh_months: Rolling window size in months (default: 12).
        parameterize: If True, inject RangeStart/RangeEnd M parameters and
                    modify partition expressions with range filters (default: True).
        optimize_dax: If True, rewrite generated measures with the DAX optimizer
                    (nested IF to SWITCH, COALESCE, constant folding). Opt-in,
                    because it changes the emitted DAX (default: False).
        time_intelligence: 'auto' to add YTD/PY/YoY% measures for aggregation
                    measures, 'none' to skip (default). Requires a date table.

    Returns:
        dict: Statistics about the generated model
    """
    if extra_objects is None:
        extra_objects = {}

    # Step 1: Build the semantic model
    model = _build_semantic_model(datasources, report_name, extra_objects,
                                  calendar_start=calendar_start,
                                  calendar_end=calendar_end,
                                  culture=culture,
                                  model_mode=model_mode,
                                  composite_threshold=composite_threshold,
                                  agg_tables=agg_tables)

    # Optimize before self-healing, so rewritten DAX still passes the same
    # validation and repair gates as directly converted DAX.
    if optimize_dax:
        _apply_dax_optimization(model)

    if (time_intelligence or 'none') == 'auto':
        _apply_time_intelligence(model)

    # Step 1b: Self-healing — validate and auto-repair common issues
    from powerbi_import.recovery_report import RecoveryReport
    recovery = RecoveryReport(report_name)
    repair_count = _self_heal_model(model, recovery=recovery)

    # Step 1b2: Incremental refresh detection & wiring (Sprint 120)
    ir_result = None
    if incremental_refresh:
        ir_result = apply_incremental_refresh(
            model, datasources=datasources,
            rolling_months=incremental_refresh_months,
            incremental_days=3,
            parameterize=parameterize,
        )
        if ir_result.get('tables_configured'):
            logger.info("Incremental refresh configured for %d table(s): %s",
                        len(ir_result['tables_configured']),
                        ', '.join(ir_result['tables_configured']))

    # Step 1c: M-partition validation gate (Sprint 129.2). Every generated
    # M expression is parsed before write; issues are logged to the recovery
    # report so operators can triage without blocking the migration.
    m_validation_issues = _validate_m_partitions(model, recovery=recovery)

    # Attach languages metadata for _write_tmdl_files
    if languages:
        model['model']['_languages'] = languages

    # Attach linguistic synonyms for Q&A support
    linguistic_synonyms = extra_objects.get('linguistic_schema', {})
    if linguistic_synonyms:
        model['model']['_linguistic_synonyms'] = linguistic_synonyms

    # Step 2a: Collect stats and symbols BEFORE writing (the writer
    #          clears column/measure data from tables to free memory).
    tables = model.get('model', {}).get('tables', [])
    rels = model.get('model', {}).get('relationships', [])

    actual_bim_measures = set()
    actual_bim_symbols = set()
    actual_bim_column_types = {}  # (tname, cname) -> normalized dataType (lowercase)
    actual_bim_measure_types = {}  # (tname, mname) -> inferred return type
    total_columns = 0
    total_measures = 0
    total_hierarchies = 0
    # First pass: collect column types so we can infer measure return types from them
    for t in tables:
        tname = t.get('name', '')
        for c in t.get('columns', []):
            cname = c.get('name', '')
            if cname:
                actual_bim_symbols.add((tname, cname))
                ct = (c.get('dataType') or '').strip().lower()
                if ct:
                    actual_bim_column_types[(tname, cname)] = ct
        total_columns += len(t.get('columns', []))
        total_hierarchies += len(t.get('hierarchies', []))
    # Second pass: infer measure return types — especially for parameter
    # measures (SELECTEDVALUE('Tbl'[Col], default)) — needed so filter
    # literals on those measures are quoted/typed correctly.  Also detects
    # string/boolean-returning measures so visual routing can avoid
    # placing them on numeric-only roles (e.g. scatter X/Y).
    _sv_re = re.compile(
        r"SELECTEDVALUE\s*\(\s*'?([^'\[]+)'?\s*\[\s*([^\]]+)\s*\]",
        re.IGNORECASE
    )
    _str_literal_re = re.compile(r'^"[^"]*"\s*$')
    _numeric_func_re = re.compile(
        r"\b(?:SUM|SUMX|AVERAGE|AVERAGEX|COUNT|COUNTA|COUNTAX|COUNTX|COUNTROWS|"
        r"DISTINCTCOUNT|MIN|MINX|MAX|MAXX|DIVIDE|RANKX|CALCULATE|"
        r"ROUND|CEILING|FLOOR|ABS|POWER|SQRT|LOG|LN|EXP|"
        r"YEAR|MONTH|DAY|HOUR|MINUTE|SECOND|WEEKDAY|WEEKNUM|QUARTER|DATEDIFF|"
        r"VAR\.S|VAR\.P|STDEV\.S|STDEV\.P|MEDIAN|PERCENTILE\.INC|PERCENTILE\.EXC)\b",
        re.IGNORECASE
    )
    _bool_op_re = re.compile(r"(?:<=|>=|<>|!=|==|\|\||&&)")
    _string_func_re = re.compile(
        r"\b(?:FORMAT|CONCATENATE|CONCATENATEX|LEFT|RIGHT|MID|UPPER|LOWER|"
        r"PROPER|TRIM|SUBSTITUTE|REPT|REPLACE|UNICHAR)\s*\(",
        re.IGNORECASE
    )

    def _infer_return_type(expr_str):
        """Best-effort static return-type inference for DAX measures.
        Returns 'string', 'boolean', or None when undetermined.

        Designed to be conservative — only commits to a type when the
        expression's *return value* is unambiguously string or boolean.
        Numeric calls used only inside conditions (e.g.
        ``IF(MAX([Date]) > x, "Erreur", "")``) do not block string inference
        as long as no numeric *return* slot is present.
        """
        if not expr_str:
            return None
        s = expr_str.strip()
        # 1. Pure string literal
        if _str_literal_re.match(s):
            return 'string'
        # Strip out string literals so we can analyze operators safely
        no_strings = re.sub(r'"[^"]*"', '""', s)
        # 2. Top-level boolean — comparison or logical operators present
        #    and no numeric/aggregation function call
        if _bool_op_re.search(no_strings) and not _numeric_func_re.search(no_strings):
            return 'boolean'
        # Cheap return-position signals
        has_string_literal = bool(re.search(r'"[^"]*"', s))
        # A numeric *return* slot: a bare number immediately before , or )
        # (after stripping strings so we don't match digits inside quoted text)
        has_numeric_return = bool(
            re.search(r',\s*-?\d+(?:\.\d+)?\s*[,)]', no_strings)
        )
        # 3. IF/SWITCH chain returning only string literals.  We allow numeric
        #    functions in the *condition* portion (DATEDIFF, MAX, TODAY ...) as
        #    long as no numeric value ever appears in a return slot.
        if re.match(r'^(?:IF|SWITCH)\s*\(', s, re.IGNORECASE):
            if has_string_literal and not has_numeric_return:
                return 'string'
        # 4. Top-level string concatenation (``"x" & "y"`` or
        #    ``IF(...) & IF(...)``) — `&` is the DAX string-concat operator.
        if has_string_literal and not has_numeric_return:
            if re.search(r'"\s*&|&\s*"|\)\s*&\s*[A-Z_"(]', s):
                return 'string'
        # 5. Pure string-function call (FORMAT/CONCATENATE/...) with no
        #    numeric-return slot inside its arguments.
        if (_string_func_re.search(s)
                and not _numeric_return_only(no_strings)):
            return 'string'
        return None

    def _numeric_return_only(no_strings_expr):
        # Helper: a numeric return slot exists.  Kept as a closure so it
        # can evolve independently from the regex flags above.
        return bool(
            re.search(r',\s*-?\d+(?:\.\d+)?\s*[,)]', no_strings_expr)
        )

    for t in tables:
        tname = t.get('name', '')
        for m in t.get('measures', []):
            mname = m.get('name', '')
            if not mname:
                continue
            actual_bim_measures.add(mname)
            actual_bim_symbols.add((tname, mname))
            expr = (m.get('expression') or '').strip()
            if not expr:
                continue
            sv = _sv_re.search(expr)
            if sv:
                ref_tbl = sv.group(1).strip()
                ref_col = sv.group(2).strip()
                ct = (actual_bim_column_types.get((ref_tbl, ref_col))
                      or actual_bim_column_types.get((tname, ref_col)))
                if ct:
                    actual_bim_measure_types[(tname, mname)] = ct
                    continue
            inferred = _infer_return_type(expr)
            if inferred:
                actual_bim_measure_types[(tname, mname)] = inferred
        total_measures += len(t.get('measures', []))

    # Step 2b: Build lineage map BEFORE writing — _write_tmdl_files clears
    #          column/measure data from tables to free memory, so lineage must
    #          be captured while the data is still intact.
    lineage = _build_lineage_map(tables, rels, extra_objects, datasources)

    # Step 2c: Write TMDL files (clears column/measure data afterward)
    _write_tmdl_files(model, output_dir)

    # Step 3: Return pre-computed stats
    stats = {
        'tables': len(tables),
        'columns': total_columns,
        'measures': total_measures,
        'relationships': len(rels),
        'hierarchies': total_hierarchies,
        'roles': len(model.get('model', {}).get('roles', [])),
        'actual_bim_measures': actual_bim_measures,
        'actual_bim_symbols': actual_bim_symbols,
        'actual_bim_column_types': actual_bim_column_types,
        'actual_bim_measure_types': actual_bim_measure_types,
        'self_heal_repairs': repair_count,
        'recovery_summary': recovery.get_summary() if recovery.has_repairs else None,
        'm_validation_issues': m_validation_issues,
        'lineage': lineage,
        'table_rename_map': model.get('_table_rename_map', {}),
    }
    if ir_result:
        stats['incremental_refresh'] = ir_result
    return stats


# Lineage mapping lives in tmdl_lineage; re-exported here so existing
# imports of these names keep working.
from powerbi_import.tmdl_lineage import (  # noqa: E402,F401
    _build_lineage_map,
    _calculation_names,
    _generated_table_names,
    _normalize_column,
)


# ════════════════════════════════════════════════════════════════════
#  SEMANTIC MODEL BUILDING
# ════════════════════════════════════════════════════════════════════

def _build_semantic_model(datasources, report_name="Report", extra_objects=None,
                          calendar_start=None, calendar_end=None, culture=None,
                          model_mode='import', composite_threshold=None,
                          agg_tables='none'):
    """
    Build a complete semantic model from extracted Tableau datasources.

    Produces tables, partitions with M queries, DAX measures, calculated
    columns, relationships, hierarchies, sets/groups/bins, parameters,
    date table, geographic data categories, hidden columns, and RLS roles.

    Orchestrator that delegates to focused sub-functions.
    """
    if extra_objects is None:
        extra_objects = {}

    effective_culture = culture or "en-US"

    is_direct_lake = (model_mode or '').lower() == 'directlake'
    model = {
        "name": report_name,
        "compatibilityLevel": 1604 if is_direct_lake else 1550,
        "model": {
            "culture": effective_culture,
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "tables": [],
            "relationships": [],
            "roles": []
        }
    }

    # Store calendar options for _add_date_table
    model['_calendar_start'] = calendar_start
    model['_calendar_end'] = calendar_end

    # Store model mode for partition generation
    model['_model_mode'] = model_mode or 'import'
    model['_composite_threshold'] = composite_threshold
    model['_agg_tables'] = agg_tables or 'none'
    model['_direct_lake'] = extra_objects.get('_direct_lake', {})
    if is_direct_lake:
        model['model']['defaultMode'] = 'directLake'

    # Store raw datasources for M parameter generation (server/database)
    model['_datasources'] = datasources

    # Phase 1-2c: Collect tables, build context mappings
    ctx = _collect_semantic_context(datasources, extra_objects)

    # Phase 3: Create tables
    _create_semantic_tables(model, ctx, datasources, extra_objects)

    # Phase 4: Create and validate relationships
    _create_and_validate_relationships(model, datasources)

    # Phases 5-12: Enrichments (sets, date table, hierarchies, params, RLS, etc.)
    _apply_semantic_enrichments(model, extra_objects, ctx['main_table_name'],
                                ctx['column_table_map'], datasources)

    # Phase 13: Composite model post-processing
    if (model_mode or 'import') == 'composite':
        _enforce_hybrid_relationship_constraints(model)
        if (agg_tables or 'none') == 'auto':
            _generate_aggregation_tables(model)

    # Attach table rename map for PBIP report generator
    model['_table_rename_map'] = ctx.get('table_rename_map', {})

    return model



# Relationship inference lives in tmdl_relationships; re-exported here so
# existing imports of these names keep working.
from powerbi_import.tmdl_relationships import (  # noqa: E402
    _build_relationships,
    _create_and_validate_relationships,
    _deactivate_ambiguous_paths,
    _detect_join_graph_issues,
    _detect_many_to_many,
    _enforce_hybrid_relationship_constraints,
    _fix_relationship_type_mismatches,
    _infer_cross_table_relationships,
    _is_parameter_table,
)


def _generate_aggregation_tables(model):
    """Generate Import-mode aggregation tables for directQuery fact tables."""
    new_tables = []
    new_rels = []
    for table in model['model']['tables']:
        partitions = table.get('partitions', [])
        if not partitions or partitions[0].get('mode') != 'directQuery':
            continue
        tname = table.get('name', '')
        measures = table.get('measures', [])
        columns = table.get('columns', [])
        if not measures:
            continue

        agg_name = f"Agg_{tname}"
        agg_columns = []
        for col in columns:
            col_type = col.get('dataType', 'string')
            if col_type in ('DateTime', 'int64', 'double', 'decimal'):
                agg_col = {
                    'name': col['name'],
                    'dataType': col_type,
                    'sourceColumn': col.get('sourceColumn', col['name']),
                    'summarizeBy': 'none',
                    'annotations': [{'name': 'alternateOf', 'value': f"'{tname}'[{col['name']}]"}],
                }
                agg_columns.append(agg_col)

        if not agg_columns:
            continue

        # M query for agg table: group-by on dimension keys, summarize measures
        dim_cols = [c['name'] for c in agg_columns if c['dataType'] in ('DateTime', 'int64')]
        m_lines = [f'let\n    Source = {tname},']
        if dim_cols:
            group_cols = ', '.join(f'"{c}"' for c in dim_cols)
            m_lines.append(f'    Grouped = Table.Group(Source, {{{group_cols}}}, {{}})')
        else:
            m_lines.append('    Grouped = Source')
        m_lines.append('in\n    Grouped')
        m_query = '\n'.join(m_lines)

        agg_table = {
            'name': agg_name,
            'columns': agg_columns,
            'partitions': [{
                'name': f"Partition-{agg_name}",
                'mode': 'import',
                'source': {'type': 'm', 'expression': m_query},
            }],
            'measures': [],
            'annotations': [{'name': 'isAggregationTable', 'value': 'true'}],
        }
        new_tables.append(agg_table)

    model['model']['tables'].extend(new_tables)


def _collect_semantic_context(datasources, extra_objects):
    """Phases 1-2c: Collect tables, deduplicate, and build DAX context mappings.

    Returns a dict with: best_tables, m_query_overrides, all_calculations,
    col_metadata_map, main_table_name, dax_context, column_table_map,
    table_datasource_set, ds_main_table, measure_names.
    """
    # Phase 1: Collect all physical tables and deduplicate
    best_tables = {}  # name -> (table_dict, connection_details)
    table_ds_origin = {}  # table_name -> ds_name (datasource that first defined it)
    table_rename_map = {}  # (ds_name, orig_table_name) -> new_table_name
    m_query_overrides = {}  # table_name -> complete M query (from Prep flows)
    all_calculations = []
    all_columns_metadata = []
    all_hierarchies = []
    all_sets = []
    all_groups = []
    all_bins = []
    _logger = logging.getLogger(__name__)

    for ds in datasources:
        ds_name = ds.get('name', '')
        ds_caption = ds.get('caption', ds_name)
        ds_connection = ds.get('connection', {})
        connection_map = ds.get('connection_map', {})
        calculations = ds.get('calculations', [])
        all_calculations.extend(calculations)

        # Collect column metadata
        ds_columns = ds.get('columns', [])
        all_columns_metadata.extend(ds_columns)

        # Extract physical columns from datasource-level list (excluding calculations)
        ds_physical_cols = [c for c in ds_columns if not c.get('calculation')]

        tables = ds.get('tables', [])
        for table in tables:
            table_name = table.get('name', 'Table1')

            # Skip tables without a name
            if not table_name or table_name == 'Unknown':
                continue

            # Inherit datasource-level columns into tables that have none
            # (common for Tableau Extracts: single table with columns at DS level)
            if not table.get('columns') and ds_physical_cols and len(tables) == 1:
                # Clean DS-level columns: strip bracket notation and skip special columns
                cleaned_cols = []
                for c in ds_physical_cols:
                    raw = c.get('name', '')
                    if raw.startswith('[:') or not raw:
                        continue  # Skip special Tableau columns (e.g. [:Measure Names])
                    clean = dict(c)
                    clean['name'] = raw.strip('[]')
                    cleaned_cols.append(clean)
                table['columns'] = cleaned_cols

            col_count = len(table.get('columns', []))

            # Resolve per-table connection
            table_conn = table.get('connection_details', {})
            if not table_conn:
                conn_ref = table.get('connection', '')
                table_conn = connection_map.get(conn_ref, ds_connection)

            # Deduplicate: merge columns only within the SAME datasource.
            # Tables with the same name from DIFFERENT datasources get a
            # datasource-prefixed name to avoid cross-datasource column mixing.
            if table_name not in best_tables:
                best_tables[table_name] = (table, table_conn)
                table_ds_origin[table_name] = ds_name
            elif table_ds_origin.get(table_name) == ds_name:
                # Same datasource — merge columns (existing behavior)
                existing_cols = best_tables[table_name][0].get('columns', [])
                existing_names = {c.get('name', '') for c in existing_cols}
                for col in table.get('columns', []):
                    if col.get('name', '') not in existing_names:
                        existing_cols.append(col)
                        existing_names.add(col.get('name', ''))
                # Keep the connection from the table with more columns originally
                if col_count > len(existing_cols) - len(table.get('columns', [])):
                    best_tables[table_name] = (best_tables[table_name][0], table_conn)
            else:
                # Different datasource — create separate table to avoid
                # mixing columns from unrelated data sources.
                ds_label = ds_caption.replace('[', '').replace(']', '')
                if ds_label == ds_name and ds_label.startswith('federated.'):
                    ds_label = ds_label.replace('federated.', '', 1)[:8]
                unique_name = f"{table_name} ({ds_label})"
                counter = 2
                while unique_name in best_tables:
                    unique_name = f"{table_name} ({ds_label} {counter})"
                    counter += 1
                table_copy = dict(table)
                table_copy['name'] = unique_name
                best_tables[unique_name] = (table_copy, table_conn)
                table_ds_origin[unique_name] = ds_name
                table_rename_map[(ds_name, table_name)] = unique_name
                _logger.info(
                    "Table '%s' from datasource '%s' renamed to '%s' to avoid "
                    "collision with same-named table from another datasource.",
                    table_name, ds_caption, unique_name,
                )

        # Collect Prep flow M query overrides
        ds_m_overrides = ds.get('m_query_overrides', {})
        for tname, mq in ds_m_overrides.items():
            m_query_overrides[tname] = mq
        # Single-table override (from prep_flow_parser output)
        single_override = ds.get('m_query_override', '')
        if single_override:
            for table in ds.get('tables', []):
                m_query_overrides[table.get('name', '')] = single_override

    # Phase 2: Identify the main table (the one with the most columns = fact table)
    main_table_name = None
    max_cols = -1
    for tname, (table, conn) in best_tables.items():
        ncols = len(table.get('columns', []))
        if ncols > max_cols:
            max_cols = ncols
            main_table_name = tname

    # Phase 2a: Build column metadata mapping
    col_metadata_map = {}
    for cm in all_columns_metadata:
        raw = cm.get('name', '').replace('[', '').replace(']', '')
        caption = cm.get('caption', raw)
        key = caption if caption else raw
        col_metadata_map[key] = cm
        col_metadata_map[raw] = cm

    # Phase 2b: Build context mappings for DAX conversion
    calc_map = {}
    for calc in all_calculations:
        raw = calc.get('name', '').replace('[', '').replace(']', '')
        caption = calc.get('caption', raw)
        if raw and raw != caption:
            calc_map[raw] = caption

    # param_map: "Parameter X" -> parameter caption
    param_map = {}
    # Source 1: From "Parameters" datasource calculations (old Tableau format)
    for ds in datasources:
        if ds.get('name', '') == 'Parameters':
            for calc in ds.get('calculations', []):
                raw = calc.get('name', '').replace('[', '').replace(']', '')
                caption = calc.get('caption', raw)
                if raw:
                    param_map[raw] = caption
    # Source 2: From extracted parameters (new Tableau format)
    for param in extra_objects.get('parameters', []):
        raw_name = param.get('name', '')
        caption = param.get('caption', '')
        if raw_name and caption:
            match = re.match(r'\[Parameters\]\.\[([^\]]+)\]', raw_name)
            if match:
                param_map[match.group(1)] = caption
            else:
                clean = raw_name.replace('[', '').replace(']', '')
                if clean and clean not in param_map:
                    param_map[clean] = caption

    # column_table_map: column_name -> table_name
    column_table_map = {}
    for tname, (table, conn) in best_tables.items():
        for col in table.get('columns', []):
            cname = col.get('name', '')
            if cname and cname not in column_table_map:
                column_table_map[cname] = tname

    # Supplement with Tableau local-name → table mappings from metadata
    # records.  Connectors like Salesforce have columns (e.g. Id →
    # "Opportunity ID", Probability → "Probability (%)") that exist in
    # <metadata-record> elements but have no <column> element at the
    # datasource level.  Without this, DAX resolution defaults to the
    # current table and produces invalid references.
    for ds in datasources:
        for col_name, parent_table in ds.get('col_local_name_map', {}).items():
            if col_name not in column_table_map and parent_table in best_tables:
                column_table_map[col_name] = parent_table

    # measure_names: set of all measure names (captions).
    # Exclude calculated columns (dimension-role calcs with column refs but no
    # aggregation) so that _resolve_columns qualifies them via column_table_map
    # instead of leaving them as bare [col] measure references.
    measure_names = set()
    _agg_pat_mn = re.compile(
        r'\b(SUM|AVG|AVERAGE|MIN|MAX|COUNT|COUNTD|MEDIAN|STDEV|STDEVP|'
        r'VAR|VARP|PERCENTILE|ATTR|CORR|COVAR|COVARP|COLLECT)\s*\(',
        re.IGNORECASE)
    for calc in all_calculations:
        caption = calc.get('caption', calc.get('name', '').replace('[', '').replace(']', ''))
        if not caption:
            continue
        role = calc.get('role', 'measure')
        formula = calc.get('formula', '').strip()
        has_agg = bool(_agg_pat_mn.search(formula)) if formula else False
        # A formula without aggregation that references columns (has [brackets])
        # is a calculated column — it needs row context.  Tableau's role
        # attribute is unreliable: role='measure' only means the field was
        # placed on a measure shelf, not that the formula is aggregated.
        has_col_brackets = bool(formula and '[' in formula)
        is_calc_col = not has_agg and (role == 'dimension' or has_col_brackets)
        if not is_calc_col:
            measure_names.add(caption)
    measure_names.update(param_map.values())
    # Tableau parameter captions can contain bracketed fragments.  The DAX
    # resolver sanitizes those captions before emitting references, so index
    # the same canonical spelling here to avoid inventing a fact-table column.
    measure_names.update(
        sanitize_param_brackets(caption) for caption in param_map.values()
    )

    # param_values: {caption: literal_value} for inlining in calculated columns
    param_values = {}
    # Simple Tableau→DAX function replacements applied to inlined literals
    _inline_replacements = [
        (re.compile(r'\bMAKEDATE\s*\(', re.IGNORECASE), 'DATE('),
        (re.compile(r'\bMAKEDATETIME\s*\(', re.IGNORECASE), 'DATE('),
        (re.compile(r'\bMAKETIME\s*\(', re.IGNORECASE), 'TIME('),
    ]
    # Literal-only filter: only inline real constants (numbers, strings,
    # booleans, DATE()/TIME() literals).  Function calls like INDEX(),
    # FIRST(), LAST(), or arbitrary expressions must NOT be inlined as bare
    # references — doing so corrupts downstream calc bodies that legitimately
    # use the same identifier (e.g. a calc named "Index()" whose body is
    # "Index()" must remain intact for the INDEX() converter).
    _literal_re = re.compile(
        r'^(?:'
        r'[-+]?\d+(?:\.\d+)?'                            # number
        r'|"(?:[^"\\]|\\.)*"'                            # double-quoted string
        r"|'(?:[^'\\]|\\.)*'"                            # single-quoted string
        r'|true|false'                                   # boolean
        r'|DATE\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)'     # DATE literal
        r'|TIME\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)'     # TIME literal
        r'|#\d{4}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}:\d{2})?#' # Tableau date literal
        r')$',
        re.IGNORECASE,
    )
    for calc in all_calculations:
        caption = calc.get('caption', calc.get('name', '').replace('[', '').replace(']', ''))
        formula = calc.get('formula', '').strip()
        if caption and formula and '[' not in formula:
            # Apply basic Tableau→DAX replacements so inlined values
            # don't contain unconverted function names (e.g. MAKEDATE→DATE).
            for pattern, repl in _inline_replacements:
                formula = pattern.sub(repl, formula)
            if _literal_re.match(formula):
                param_values[caption] = formula
    for param in extra_objects.get('parameters', []):
        caption = param.get('caption', '')
        value = param.get('value', '').strip('"')
        if caption and value and caption not in param_values:
            datatype = param.get('datatype', 'string')
            if datatype == 'string':
                param_values[caption] = f'"{value}"'
            elif datatype in ('date', 'datetime'):
                # Convert Tableau #YYYY-MM-DD# date literal to DAX DATE()
                date_m = re.match(r'#(\d{4})-(\d{2})-(\d{2})#', value)
                if date_m:
                    param_values[caption] = f'DATE({int(date_m.group(1))}, {int(date_m.group(2))}, {int(date_m.group(3))})'
                else:
                    param_values[caption] = value
            else:
                param_values[caption] = value

    # Also add parameter measure names
    for param in extra_objects.get('parameters', []):
        caption = param.get('caption', '')
        if caption:
            measure_names.add(caption)
            measure_names.add(sanitize_param_brackets(caption))

    # Phase 2c: Build per-datasource column → table map for multi-source routing
    # Maps datasource_name → {column_name → table_name}
    ds_column_table_map = {}
    datasource_table_map = {}  # table_name → datasource_name (last wins for conn)
    table_datasource_set = {}  # table_name → set of ALL datasource names that own it
    for ds in datasources:
        ds_name = ds.get('name', '')
        ds_col_map = {}
        for table in ds.get('tables', []):
            tname = table.get('name', 'Table1')
            # Use renamed table name if this DS caused a collision
            actual_tname = table_rename_map.get((ds_name, tname), tname)
            if actual_tname in best_tables:
                datasource_table_map[actual_tname] = ds_name
                # Track ALL datasources that own this table (for calculation routing)
                if actual_tname not in table_datasource_set:
                    table_datasource_set[actual_tname] = set()
                table_datasource_set[actual_tname].add(ds_name)
                for col in table.get('columns', []):
                    cname = col.get('name', '')
                    if cname:
                        ds_col_map[cname] = actual_tname
        if ds_name:
            ds_column_table_map[ds_name] = ds_col_map

    dax_context = {
        'calc_map': calc_map,
        'param_map': param_map,
        'column_table_map': column_table_map,
        'measure_names': measure_names,
        'param_values': param_values,
        'ds_column_table_map': ds_column_table_map,
        'datasource_table_map': datasource_table_map,
    }

    # Build compute_using_map from worksheet table_calcs for PARTITIONBY/ORDERBY
    # Maps calc field name → list of compute-using dimension names
    compute_using_map = {}
    for ws in extra_objects.get('worksheets', []):
        for tc in ws.get('table_calcs', []):
            tc_field = tc.get('field', '')
            cu = tc.get('compute_using', [])
            if tc_field and cu:
                compute_using_map[tc_field] = cu
    dax_context['compute_using_map'] = compute_using_map

    # Build ds_main_table: datasource_name → table_name (table with most columns in that DS)
    ds_main_table = {}
    for tname, ds_names in table_datasource_set.items():
        if tname not in best_tables:
            continue
        for ds_name in ds_names:
            if ds_name not in ds_main_table:
                ds_main_table[ds_name] = tname
            else:
                existing = ds_main_table[ds_name]
                existing_cols = len(best_tables.get(existing, ({}, {}))[0].get('columns', []))
                current_cols = len(best_tables.get(tname, ({}, {}))[0].get('columns', []))
                if current_cols > existing_cols:
                    ds_main_table[ds_name] = tname

    # Register dimension-role calculations (calculated columns) in the
    # column_table_map so that cross-table references resolve correctly.
    # Without this, SUMX('OtherTable', IF([CalcCol], ...)) leaves [CalcCol]
    # unqualified, causing "column not found" errors in DAX.
    _agg_pat = re.compile(
        r'\b(SUM|AVG|AVERAGE|MIN|MAX|COUNT|COUNTD|MEDIAN|STDEV|STDEVP|'
        r'VAR|VARP|PERCENTILE|ATTR|CORR|COVAR|COVARP|COLLECT)\s*\(',
        re.IGNORECASE)
    for calc in all_calculations:
        role = calc.get('role', 'measure')
        formula = calc.get('formula', '').strip()
        if not formula:
            continue
        caption = calc.get('caption', calc.get('name', '').replace('[', '').replace(']', ''))
        if not caption or caption in column_table_map:
            continue
        has_agg = bool(_agg_pat.search(formula))
        has_col_refs = bool(re.search(r'\[', formula))
        is_calc_col = (role == 'dimension') or (role == 'measure' and not has_agg and has_col_refs)
        if is_calc_col:
            # Route to the datasource's main table
            dsn = calc.get('datasource_name', '')
            target_table = ds_main_table.get(dsn, main_table_name)
            column_table_map[caption] = target_table

    return {
        'best_tables': best_tables,
        'm_query_overrides': m_query_overrides,
        'all_calculations': all_calculations,
        'col_metadata_map': col_metadata_map,
        'main_table_name': main_table_name,
        'dax_context': dax_context,
        'column_table_map': column_table_map,
        'table_datasource_set': table_datasource_set,
        'ds_main_table': ds_main_table,
        'measure_names': measure_names,
        'datasource_table_map': datasource_table_map,
        'table_rename_map': table_rename_map,
    }


def _create_semantic_tables(model, ctx, datasources, extra_objects=None):
    """Phase 3: Create model tables with calculation routing."""
    best_tables = ctx['best_tables']
    all_calculations = ctx['all_calculations']
    main_table_name = ctx['main_table_name']
    table_datasource_set = ctx['table_datasource_set']
    ds_main_table = ctx['ds_main_table']
    dax_context = ctx['dax_context']
    col_metadata_map = ctx['col_metadata_map']
    m_query_overrides = ctx['m_query_overrides']
    datasource_table_map = ctx['datasource_table_map']

    # Build hyper table lookup from extracted hyper_files metadata
    hyper_table_data = {}  # table_name_lower -> hyper_reader_tables list
    if extra_objects:
        for hf in extra_objects.get('hyper_files', []):
            hrt = hf.get('hyper_reader_tables', [])
            if hrt:
                for ht in hrt:
                    tname = ht.get('table', '')
                    if tname:
                        hyper_table_data[tname.lower()] = hrt

    for table_name, (table, table_conn) in best_tables.items():
        # An explicit table attribution wins: a merged model records which
        # table each calculation came from, and datasource routing alone would
        # send them all to the widest table in the merged datasource.
        attributed = [c for c in all_calculations if c.get('table') == table_name]
        unattributed = [c for c in all_calculations if not c.get('table')]

        # Route calculations to their source datasource's main table
        # Use table_datasource_set to handle multiple datasources sharing the same table name
        ds_names_for_table = table_datasource_set.get(table_name, set())
        is_main_for_any_ds = any(
            ds_main_table.get(dsn) == table_name for dsn in ds_names_for_table
        )
        if is_main_for_any_ds:
            # This table is the main table for one or more datasources — collect all their calcs
            owning_ds_names = {
                dsn for dsn in ds_names_for_table
                if ds_main_table.get(dsn) == table_name
            }
            table_calculations = [
                c for c in unattributed
                if c.get('datasource_name', '') in owning_ds_names
            ]
            # Also add calcs with no datasource_name (legacy) if this is the global main table
            if table_name == main_table_name:
                table_calculations += [
                    c for c in unattributed
                    if not c.get('datasource_name')
                ]
        elif table_name == main_table_name:
            # Fallback: calcs with no datasource match go to the global main table
            routed_ds_names = set(ds_main_table.values())
            table_calculations = [
                c for c in unattributed
                if c.get('datasource_name', '') not in datasource_table_map.values()
                or not c.get('datasource_name')
            ]
        else:
            table_calculations = []

        table_calculations = attributed + [
            c for c in table_calculations if c not in attributed
        ]

        tbl = _build_table(
            table=table,
            connection=table_conn,
            calculations=table_calculations,
            columns_metadata=[],
            dax_context=dax_context,
            col_metadata_map=col_metadata_map,
            extra_objects={},
            m_query_override=m_query_overrides.get(table_name, ''),
            model_mode=model.get('_model_mode', 'import'),
            composite_threshold=model.get('_composite_threshold'),
        )

        # Sprint 109: If this is a hyper/extract table with no Prep override,
        # try to inject inline data from extracted .hyper files
        conn_type = table_conn.get('type', '')
        if conn_type.lower() in ('hyper', 'extract', 'dataengine') \
                and not m_query_overrides.get(table_name):
            hrt = hyper_table_data.get(table_name.lower())
            if hrt:
                hyper_m = generate_m_from_hyper(hrt, table_name=table_name)
                if hyper_m:
                    # Replace the partition's M expression with hyper-inlined data
                    partitions = tbl.get('partitions', [])
                    if partitions:
                        partitions[0]['source']['expression'] = hyper_m
                        logger.debug("Hyper data inlined for table '%s'", table_name)

        model["model"]["tables"].append(tbl)




def _apply_semantic_enrichments(model, extra_objects, main_table_name, column_table_map, datasources):
    """Phases 5-12: Sets, date table, hierarchies, parameters, RLS, cross-table inference, perspectives."""
    is_direct_lake = model.get('_model_mode', '').lower() == 'directlake'

    # Phase 5: Add sets, groups, bins as calculated columns
    if not is_direct_lake:
        _process_sets_groups_bins(model, extra_objects, main_table_name, column_table_map)

    # Phase 6: Automatic date table if date columns detected
    # Skip if the source already has a date/calendar table (name-based or column-heuristic)
    has_existing_date_table = any(
        _is_date_table(t) for t in model['model']['tables']
    )

    has_date_columns = False
    if not has_existing_date_table:
        for table in model["model"]["tables"]:
            for col in table.get("columns", []):
                if col.get("dataType") == "DateTime" or col.get("dataCategory") == "DateTime":
                    has_date_columns = True
                    break
            if has_date_columns:
                break
    if has_date_columns and not has_existing_date_table and not is_direct_lake:
        _add_date_table(model)

    # Phase 7: Hierarchies from Tableau drill-paths
    _apply_hierarchies(model, extra_objects.get('hierarchies', []), column_table_map)

    # Phase 7b: Auto-generate date hierarchies for DateTime columns without one
    _auto_date_hierarchies(model)

    # Phase 8: Parameter tables (What-If parameters)
    if not is_direct_lake:
        _create_parameter_tables(model, extra_objects.get('parameters', []), main_table_name)

    # Phase 8b: Calculation groups (measure-switching parameters)
    if not is_direct_lake:
        _create_calculation_groups(model, extra_objects.get('parameters', []), main_table_name)

    # Phase 8c: Field parameters (dimension-switching parameters with NAMEOF)
    if not is_direct_lake:
        _create_field_parameters(model, extra_objects.get('parameters', []),
                                 main_table_name, column_table_map)

    # Phase 9: RLS roles from Tableau user filters / security
    _create_rls_roles(model, extra_objects.get('user_filters', []),
                      main_table_name, column_table_map)

    # Phase 9b: Auto-generate measures for quick table calculations (% of total, running sum, etc.)
    _create_quick_table_calc_measures(model, extra_objects.get('worksheets', []),
                                      main_table_name, column_table_map)

    # Phase 9c: Auto-generate "Number of Records" COUNTROWS measure when
    # worksheets use COUNT(*) on __tableau_internal_object_id__.
    _create_number_of_records_measure(model, extra_objects.get('_worksheets', []),
                                      main_table_name)

    # Phase 9d: Guard against Number of Records name collision
    # (measure + column with same name in same table), which Power BI rejects.
    _remove_conflicting_number_of_records_measures(model)

    # Phase 10: Infer missing relationships from cross-table DAX references
    _infer_cross_table_relationships(model)

    # Phase 10b: Detect cardinality (runs AFTER Phase 10 so inferred rels are included)
    _detect_many_to_many(model, datasources)

    # Phase 10c: Replace RELATED() with LOOKUPVALUE() for manyToMany
    _fix_related_for_many_to_many(model)

    # Phase 11: Deactivate relationships that create ambiguous paths
    _deactivate_ambiguous_paths(model)

    # Deduplicate measures globally with case-insensitive uniqueness.
    # PBI measure names must be globally unique and are compared
    # case-insensitively, so 'index' and 'Index' collide and the model
    # fails to load.  Within a table, drop exact (case-insensitive)
    # duplicates; across tables, namespace the later measure with a
    # table-name suffix so both survive and the model still opens.
    global_measure_names = set()  # case-folded names
    for table in model["model"]["tables"]:
        tname = table.get("name", "")
        seen_in_table = set()  # case-folded names within this table
        unique_measures = []
        for measure in table.get("measures", []):
            mname = measure.get("name", "")
            key = mname.casefold()
            if key in seen_in_table:
                # Same-table duplicate → drop the second occurrence
                print(f"  ⚕ Self-heal: Dropped duplicate measure '{mname}' in '{tname}'")
                continue
            if key in global_measure_names:
                # Cross-table case collision → namespace to keep both
                new_name = f"{mname} ({tname})"
                nkey = new_name.casefold()
                suffix = 2
                while nkey in global_measure_names or nkey in seen_in_table:
                    new_name = f"{mname} ({tname}) {suffix}"
                    nkey = new_name.casefold()
                    suffix += 1
                print(f"  ⚕ Self-heal: Renamed colliding measure '{mname}' → '{new_name}'")
                _rewrite_bare_measure_references(model, mname, new_name)
                measure["name"] = new_name
                key = nkey
            seen_in_table.add(key)
            global_measure_names.add(key)
            unique_measures.append(measure)
        table["measures"] = unique_measures

    # Deduplicate columns globally with case-insensitive uniqueness (Bug #2b).
    # Column names within a table must also be unique case-insensitively.
    # When internal apostrophes create duplicates (e.g. 'Date d\'émission' becomes
    # 'Date d\'' due to truncation), deduplicate case-insensitively within each table.
    for table in model["model"]["tables"]:
        tname = table.get("name", "")
        seen_cols = {}  # casefold → original column dict
        unique_cols = []
        for col in table.get("columns", []):
            cname = col.get("name", "")
            key = cname.casefold()
            if key in seen_cols:
                # Duplicate column name (case-insensitive) → skip
                print(f"  ⚕ Self-heal: Dropped duplicate column '{cname}' in '{tname}'")
                continue
            seen_cols[key] = col
            unique_cols.append(col)
        table["columns"] = unique_cols

    # Bug #22: Detect measure/column name collisions (case-insensitive)
    # Power BI forbids the same name (case-insensitive) for a measure and column → model fails to load
    all_column_names = {}  # casefold → (table_name, column_name)
    for table in model["model"]["tables"]:
        tname = table.get("name", "")
        for col in table.get("columns", []):
            cname = col.get("name", "")
            key = cname.casefold()
            if key not in all_column_names:
                all_column_names[key] = (tname, cname)
    
    # Check measures against column names
    for table in model["model"]["tables"]:
        tname = table.get("name", "")
        for measure in table.get("measures", []):
            mname = measure.get("name", "")
            key = mname.casefold()
            if key in all_column_names:
                col_table, col_name = all_column_names[key]
                new_name = f"{mname} (Measure)"
                _rewrite_bare_measure_references(model, mname, new_name)
                measure["name"] = new_name
                print(f"  ⚕ Self-heal: Renamed measure '{mname}' → '{new_name}' (collision with column '{col_name}' in '{col_table}')")

    # Phase 12: Auto-generate perspectives from table list
    all_table_names = [t.get('name', '') for t in model["model"]["tables"]]
    model["model"]["perspectives"] = [{
        "name": "Full Model",
        "tables": all_table_names
    }]

    # Phase 12b (Sprint 123): R² measures for trend lines with show_r_squared
    worksheets = extra_objects.get('worksheets', extra_objects.get('_worksheets', []))
    if worksheets:
        _inject_r_squared_measures(model, worksheets, main_table_name, column_table_map)

    # Phase 12c (Sprint 124): Dynamic format string measures
    _inject_dynamic_format_measures(model)

    # Phase 12d: Named measures carrying Tableau field aliases
    _inject_alias_measures(model, extra_objects.get('aliases', {}),
                           column_table_map)
    # Phase 12e: Value-level Tableau aliases become a display-only column
    _inject_value_alias_columns(model, extra_objects.get('aliases', {}),
                               column_table_map)
    _inject_url_action_categories(model, extra_objects.get('actions', []),
                                  column_table_map,
                                  extra_objects.get('calculations', []))


def _value_alias_literal(value):
    """Format a Tableau alias value as a DAX literal in a SWITCH expression."""
    if value is None:
        return 'BLANK()'
    if isinstance(value, bool):
        return 'TRUE()' if value else 'FALSE()'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Keep Windows/Power BI formatting stable for small numeric aliases.
        return str(value)
    text = str(value)
    if text.lower() in {'%null%', 'null', 'blank'}:
        return 'BLANK()'
    return f'"{text.replace("\"", "\"\"")}"'


def _inject_url_action_categories(model, actions, column_table_map,
                                  calculations=None):
    """Mark direct Tableau URL-action fields as Power BI WebUrl columns."""
    if not isinstance(actions, list):
        return

    field_ref = re.compile(r'<\[[^]]+\]\.\[([^]]+)\]>')
    tables = {t.get('name', ''): t for t in model.get('model', {}).get('tables', [])}
    calculation_names = {}
    for calculation in calculations or []:
        if not isinstance(calculation, dict):
            continue
        raw_name = str(calculation.get('name', '')).strip('[]')
        caption = (calculation.get('caption') or '').strip()
        if raw_name and caption:
            calculation_names[raw_name] = caption
    for action in actions:
        if not isinstance(action, dict) or action.get('type') != 'url':
            continue
        match = field_ref.fullmatch((action.get('url') or '').strip())
        if not match:
            continue
        field_name = calculation_names.get(match.group(1), match.group(1))
        table_name = column_table_map.get(field_name)
        table = tables.get(table_name)
        if not table:
            continue
        for column in table.get('columns', []):
            if column.get('name') == field_name:
                column['dataCategory'] = 'WebUrl'
                break


def _inject_value_alias_columns(model, aliases, column_table_map):
    """Create display columns for Tableau value aliases.

    Tableau renames individual values such as "North" -> "North (N)" without
    changing the underlying field. Power BI has no direct "per-value alias"
    object, so the closest equivalent is a calculated text column that swaps the
    display value while preserving the original column for filtering and joins.
    """
    if not isinstance(aliases, dict) or not aliases:
        return

    tables = model.get('model', {}).get('tables', [])
    by_name = {t.get('name', ''): t for t in tables}
    taken = {c.get('name', '') for t in tables for c in t.get('columns', [])}

    for field_name, alias_map in aliases.items():
        if field_name == ':Measure Names' or not isinstance(alias_map, dict):
            continue

        table_name = column_table_map.get(field_name)
        if not table_name:
            continue
        table = by_name.get(table_name)
        if not table:
            continue
        if not any(c.get('name') == field_name for c in table.get('columns', [])):
            continue

        base_name = f'{field_name} (Display)'
        candidate = base_name
        suffix = 1
        while candidate in taken:
            candidate = f'{field_name} (Display {suffix})'
            suffix += 1

        cases = []
        for original_value, display_value in alias_map.items():
            if original_value is None:
                original_value = '%null%'
            literal_value = _value_alias_literal(original_value)
            literal_alias = _value_alias_literal(display_value)
            cases.append(f'{literal_value}, {literal_alias}')

        if not cases:
            continue

        field_ref = f"'{table_name}'[{field_name}]"
        expr = f"SWITCH({field_ref}, {', '.join(cases)}, {field_ref})"
        table.setdefault('columns', []).append({
            'name': candidate,
            'dataType': 'String',
            'sourceColumn': candidate,
            'summarizeBy': 'none',
            'displayFolder': 'Aliases',
            'expression': expr,
            'isCalculated': True,
            'description': f'Tableau display-name override for {field_name}',
            'annotations': [{
                'name': 'MigrationNote',
                'value': f'Generated from Tableau value aliases for {field_name}',
            }],
        })
        taken.add(candidate)


def _inject_r_squared_measures(model, worksheets, main_table_name, column_table_map):
    """Sprint 123: Generate R² DAX measures for trend lines with show_r_squared.

    For each worksheet with a trend line showing R², creates a measure using
    POWER(CORREL(x, y), 2) to compute the coefficient of determination.
    """
    existing = set()
    for t in model['model']['tables']:
        for m in t.get('measures', []):
            existing.add(m.get('name', ''))

    main_table = None
    for t in model['model']['tables']:
        if t.get('name') == main_table_name:
            main_table = t
            break
    if main_table is None and model['model']['tables']:
        main_table = model['model']['tables'][0]
    if main_table is None:
        return

    for ws in worksheets:
        if isinstance(ws, str):
            continue
        trend_lines = ws.get('trend_lines', [])
        if not trend_lines:
            continue
        for tl in trend_lines:
            if not tl.get('show_r_squared'):
                continue
            # Find a numeric measure from the worksheet fields
            ws_name = ws.get('name', ws.get('title', 'Sheet'))
            measure_name = f"R² {ws_name}"
            if measure_name in existing:
                continue
            existing.add(measure_name)

            # Look for measure fields in the worksheet
            fields = ws.get('fields', [])
            measure_field = None
            dim_field = None
            for f in fields:
                fname = f if isinstance(f, str) else f.get('name', f.get('field', ''))
                if not fname:
                    continue
                clean = fname.strip('[]').split('.')[-1].strip('[]')
                # Check if it's a measure
                is_measure = False
                for t in model['model']['tables']:
                    for m in t.get('measures', []):
                        if m.get('name') == clean:
                            is_measure = True
                            break
                    if is_measure:
                        break
                if is_measure and not measure_field:
                    measure_field = clean
                elif not dim_field:
                    dim_field = clean

            if measure_field:
                tbl = column_table_map.get(measure_field, main_table_name)
                r2_expr = (
                    f"VAR _x = RANKX(ALL('{tbl}'), [{measure_field}],,ASC,Dense) "
                    f"VAR _corr = POWER(CORREL(ADDCOLUMNS(ALL('{tbl}'), "
                    f"\"_rank\", RANKX(ALL('{tbl}'), [{measure_field}],,ASC,Dense), "
                    f"\"_val\", [{measure_field}]), [_rank], [_val]), 2) "
                    f"RETURN _corr"
                )
                main_table['measures'].append({
                    'name': measure_name,
                    'expression': f"POWER(CORREL(ADDCOLUMNS(ALL('{tbl}'), \"_idx\", RANKX(ALL('{tbl}'), [{measure_field}],,ASC,Dense)), [_idx], [{measure_field}]), 2)",
                    'formatString': '0.0000',
                    'displayFolder': 'Analytics',
                    'description': f'R² coefficient of determination for {ws_name} trend line',
                    'annotations': [{'name': 'MigrationNote',
                                     'value': f'Auto-generated R² measure for Tableau trend line on {ws_name}'}],
                })


#: Tableau aggregation prefixes in a field reference, and their DAX function.
_ALIAS_AGG_DAX = {
    'sum': 'SUM', 'avg': 'AVERAGE', 'min': 'MIN', 'max': 'MAX',
    'cnt': 'COUNT', 'ctd': 'DISTINCTCOUNT', 'median': 'MEDIAN',
    'stdev': 'STDEV.S', 'stdevp': 'STDEV.P', 'var': 'VAR.S', 'varp': 'VAR.P',
}

#: Prefixes that qualify an aggregation rather than performing one.
_ALIAS_RANK_PREFIXES = {'rank', 'rank_unique', 'rank_dense', 'rank_modified'}

#: Trailing tokens that describe the field's role, not its name.
_ALIAS_TYPE_SUFFIXES = {'qk', 'nk', 'ok'}


def _parse_alias_field_ref(raw):
    """Split a Tableau field reference into its aggregations and field name.

    Field names may themselves contain colons (``H: Life exp (years)``), so the
    known aggregation prefixes are consumed from the left and the role suffix
    from the right; whatever remains is the name.

    Args:
        raw: A reference such as ``"[Datasource].[rank:sum:F: GDP (curr $):qk]"``.

    Returns:
        tuple: ``(aggregations, field_name)``, or ``([], '')`` when unparseable.
    """
    inner = (raw or '').strip().strip('"')
    if '].[' in inner:
        inner = inner.split('].[', 1)[1]
    inner = inner.strip('[]')
    if not inner:
        return [], ''

    parts = inner.split(':')
    aggregations = []
    while parts and parts[0].lower() in (_ALIAS_AGG_DAX.keys() | _ALIAS_RANK_PREFIXES):
        aggregations.append(parts.pop(0).lower())
    if parts and parts[-1].lower() in _ALIAS_TYPE_SUFFIXES:
        parts.pop()

    return aggregations, ':'.join(parts).strip()


def _inject_alias_measures(model, aliases, column_table_map):
    """Restore the display names a Tableau author gave to aggregated fields.

    Tableau aggregates implicitly, so ``sum:F: GDP (curr $)`` renamed to
    "GDP (US $'s)" has no named counterpart to carry the alias. An explicit
    measure is added instead of renaming anything, so no existing reference
    breaks.
    """
    measure_aliases = (aliases or {}).get(':Measure Names', {})
    if not isinstance(measure_aliases, dict) or not measure_aliases:
        return

    tables = model['model']['tables']
    taken = {m.get('name', '') for t in tables for m in t.get('measures', [])}
    taken |= {c.get('name', '') for t in tables for c in t.get('columns', [])}
    by_name = {t.get('name', ''): t for t in tables}

    for raw, alias in measure_aliases.items():
        alias = (alias or '').strip()
        if not alias or alias in taken:
            continue

        aggregations, field = _parse_alias_field_ref(raw)
        aggregation = next((a for a in aggregations if a in _ALIAS_AGG_DAX), '')
        if not field or not aggregation:
            continue

        table_name = column_table_map.get(field)
        table = by_name.get(table_name)
        if not table or not any(c.get('name') == field
                                for c in table.get('columns', [])):
            continue

        safe_table = table_name.replace("'", "''")
        expression = f"{_ALIAS_AGG_DAX[aggregation]}('{safe_table}'[{field}])"
        summary = f'{aggregation.upper()} of {field}'
        if any(a in _ALIAS_RANK_PREFIXES for a in aggregations):
            expression = f"RANKX(ALL('{safe_table}'), {expression})"
            summary = f'rank by {summary}'

        table.setdefault('measures', []).append({
            'name': alias,
            'expression': expression,
            'displayFolder': 'Measures',
            'description': f'Tableau display name for {summary}',
            'annotations': [{
                'name': 'MigrationNote',
                'value': f'Restored from Tableau field alias: {raw}',
            }],
        })
        taken.add(alias)


def _inject_dynamic_format_measures(model):
    """Sprint 124: Wrap measures with conditional FORMAT() when format metadata suggests dynamic patterns.

    Detects measures whose format suggests conditional formatting:
    - Currency measures with large values → K/M/B abbreviation wrapper
    - Ratio measures → percentage vs decimal depending on magnitude
    """
    for table in model['model']['tables']:
        for measure in table.get('measures', []):
            fmt = measure.get('formatString', '')
            expr = measure.get('expression', '')
            name = measure.get('name', '')
            if not fmt or not expr:
                continue

            # Skip if already a FORMAT wrapper or a time-intelligence measure.
            # Detected structurally (TI DAX functions or the Time Intelligence
            # display folder) — never by matching specific measure names.
            _ti_funcs = ('TOTALYTD', 'TOTALQTD', 'TOTALMTD', 'DATESYTD',
                         'DATESQTD', 'DATESMTD', 'SAMEPERIODLASTYEAR', 'DATEADD',
                         'PARALLELPERIOD', 'PREVIOUSYEAR', 'PREVIOUSMONTH',
                         'PREVIOUSQUARTER', 'NEXTYEAR', 'DATESBETWEEN')
            _expr_up = expr.upper()
            if ('FORMAT(' in expr
                    or measure.get('displayFolder', '') == 'Time Intelligence'
                    or any(fn in _expr_up for fn in _ti_funcs)):
                continue

            # K/M/B abbreviation for large currency/numeric measures
            if fmt.startswith('$') or fmt.startswith('€') or fmt.startswith('£'):
                symbol = fmt[0]
                fmt_name = f"{name} Formatted"
                # Check if a formatted wrapper already exists
                existing_names = {m.get('name', '') for m in table.get('measures', [])}
                if fmt_name in existing_names:
                    continue
                table['measures'].append({
                    'name': fmt_name,
                    'expression': (
                        f'VAR _val = [{name}] '
                        f'RETURN IF(ABS(_val) >= 1E9, FORMAT(_val / 1E9, "#,0.0") & "B", '
                        f'IF(ABS(_val) >= 1E6, FORMAT(_val / 1E6, "#,0.0") & "M", '
                        f'IF(ABS(_val) >= 1E3, FORMAT(_val / 1E3, "#,0.0") & "K", '
                        f'FORMAT(_val, "{fmt}"))))'
                    ),
                    'formatString': '',
                    'displayFolder': 'Formatted',
                    'description': f'Dynamic {symbol} abbreviation for {name} (K/M/B)',
                    'annotations': [{'name': 'MigrationNote',
                                     'value': f'Auto-generated dynamic format wrapper for {name}'}],
                })
                continue

            # Percentage/ratio measures — wrap in conditional FORMAT
            # If format is percentage but expression doesn't already divide by 100
            if '%' in fmt and 'DIVIDE' in expr.upper():
                fmt_name = f"{name} Formatted"
                existing_names = {m.get('name', '') for m in table.get('measures', [])}
                if fmt_name in existing_names:
                    continue
                table['measures'].append({
                    'name': fmt_name,
                    'expression': (
                        f'VAR _val = [{name}] '
                        f'RETURN IF(ABS(_val) <= 1, FORMAT(_val, "0.0%"), '
                        f'FORMAT(_val, "#,0.00"))'
                    ),
                    'formatString': '',
                    'displayFolder': 'Formatted',
                    'description': f'Dynamic ratio/percentage format for {name}',
                    'annotations': [{'name': 'MigrationNote',
                                     'value': f'Auto-generated ratio format wrapper for {name}'}],
                })
                continue

            # Plain numeric with large values → K/M/B abbreviation
            if fmt in ('#,0', '#,0.00', '0', '0.00') and 'SUM' in expr.upper():
                fmt_name = f"{name} Formatted"
                existing_names = {m.get('name', '') for m in table.get('measures', [])}
                if fmt_name in existing_names:
                    continue
                table['measures'].append({
                    'name': fmt_name,
                    'expression': (
                        f'VAR _val = [{name}] '
                        f'RETURN IF(ABS(_val) >= 1E9, FORMAT(_val / 1E9, "#,0.0") & "B", '
                        f'IF(ABS(_val) >= 1E6, FORMAT(_val / 1E6, "#,0.0") & "M", '
                        f'IF(ABS(_val) >= 1E3, FORMAT(_val / 1E3, "#,0.0") & "K", '
                        f'FORMAT(_val, "{fmt}"))))'
                    ),
                    'formatString': '',
                    'displayFolder': 'Formatted',
                    'description': f'Dynamic numeric abbreviation for {name} (K/M/B)',
                    'annotations': [{'name': 'MigrationNote',
                                     'value': f'Auto-generated numeric format wrapper for {name}'}],
                })






def _build_table(table, connection, calculations, columns_metadata, dax_context=None,
                 col_metadata_map=None, extra_objects=None, m_query_override='',
                 model_mode='import', composite_threshold=None):
    """
    Create a semantic model table with columns, partitions and measures.

    Args:
        table: Dict with name, columns
        connection: Dict with type and connection details
        calculations: List of Tableau calculations
        columns_metadata: List of column metadata
        dax_context: Dict with calc_map, param_map, column_table_map, measure_names
        col_metadata_map: Dict {col_name: {hidden, semantic_role, description, ...}}
        extra_objects: Dict with sets, groups, bins, aliases
        model_mode: 'import', 'directquery', or 'composite'

    Returns:
        dict: Complete table definition
    """
    if dax_context is None:
        dax_context = {}
    if col_metadata_map is None:
        col_metadata_map = {}
    if extra_objects is None:
        extra_objects = {}

    table_name = table.get('name', 'Table1')
    columns = table.get('columns', [])

    # Apply DS-level type overrides to columns BEFORE M query generation
    # so that sample data in #table matches the BIM dataType.
    for col in columns:
        cname = col.get('name', '')
        meta = col_metadata_map.get(cname, {})
        ds_dt = meta.get('datatype', '')
        if ds_dt and ds_dt != col.get('datatype', ''):
            col['datatype'] = ds_dt

    is_direct_lake = (model_mode or '').lower() == 'directlake'
    if not is_direct_lake:
        # Generate M query: use Prep flow override if available, else generate from connection
        if m_query_override:
            m_query = m_query_override
        else:
            m_query = generate_power_query_m(connection, table)

        # Inject TWB-embedded transformation steps from column metadata
        m_steps = _build_m_transform_steps(columns, col_metadata_map)
        if m_steps:
            m_query = inject_m_steps(m_query, m_steps)

        # Wrap Source step with try...otherwise for graceful error handling
        col_names = [c.get('name', '') for c in columns if c.get('name')]
        m_query = wrap_source_with_try_otherwise(m_query, col_names)

    # Determine partition mode based on model_mode
    # For composite: large tables use directQuery, small/lookup use import
    partition_mode = model_mode if model_mode in ('import', 'directQuery') else 'import'
    if model_mode == 'composite':
        threshold = composite_threshold if composite_threshold is not None else 10
        col_count = len(columns)
        if col_count > threshold:
            partition_mode = 'directQuery'
        else:
            partition_mode = 'import'

    if is_direct_lake:
        from .fabric_naming import sanitize_table_name
        partition = {
            "name": f"Partition-{table_name}",
            "mode": "directLake",
            "source": {
                "type": "entity",
                "entityName": sanitize_table_name(table_name),
                "schemaName": "dbo",
                "expressionSource": "DatabaseQuery",
            },
        }
    else:
        partition = {
            "name": f"Partition-{table_name}",
            "mode": partition_mode,
            "source": {
                "type": "m",
                "expression": m_query,
            },
        }

    result_table = {
        "name": table_name,
        "columns": [],
        "partitions": [partition],
        "measures": []
    }

    # Track column names (avoid duplicates within the table)
    column_name_counts = {}

    # Add columns
    for col in columns:
        original_col_name = col.get('name', 'Column')

        # Handle duplicate column names by adding a suffix
        if original_col_name in column_name_counts:
            column_name_counts[original_col_name] += 1
            unique_col_name = f"{original_col_name}_{column_name_counts[original_col_name]}"
        else:
            column_name_counts[original_col_name] = 0
            unique_col_name = original_col_name

        # Determine data type — prefer DS-level metadata over table-level
        # because Tableau's datasource XML carries the semantic type override
        # (e.g. a hyper column typed 'string' may actually be 'real' in the DS).
        col_meta = col_metadata_map.get(unique_col_name, col_metadata_map.get(col.get('name', ''), {}))
        col_datatype = col.get('datatype', 'string')
        ds_datatype = col_meta.get('datatype', '')
        if ds_datatype and ds_datatype != col_datatype:
            col_datatype = ds_datatype

        bim_column = {
            "name": unique_col_name,
            "dataType": map_tableau_to_powerbi_type(col_datatype),
            "sourceColumn": col.get('name', 'Column'),
            "summarizeBy": "none"
        }

        # Apply metadata (hidden, semantic_role, description)
        if col_meta.get('hidden', False):
            bim_column["isHidden"] = True
        if col_meta.get('description', ''):
            bim_column["description"] = col_meta['description']

        # Geographic data categories from semantic-role
        semantic_role = col_meta.get('semantic_role', '')
        geo_category = _map_semantic_role_to_category(semantic_role, unique_col_name)
        if geo_category:
            bim_column["dataCategory"] = geo_category

        # Add the appropriate data type
        if col.get('datatype') == 'date' or col.get('datatype') == 'datetime':
            bim_column["dataCategory"] = "DateTime"
            bim_column["formatString"] = "General Date"
        elif col.get('datatype') in ['integer', 'real']:
            bim_column["summarizeBy"] = "sum"
            if col.get('datatype') == 'real':
                bim_column["formatString"] = "#,0.00"

        # Apply Tableau number format if available (overrides default)
        tableau_fmt = col_meta.get('default_format', '') or col.get('default_format', '')
        if tableau_fmt:
            pbi_fmt = _convert_tableau_format_to_pbi(tableau_fmt)
            if pbi_fmt:
                bim_column["formatString"] = pbi_fmt

        result_table["columns"].append(bim_column)

    # Separate calculations into calculated columns vs measures
    column_table_map = dax_context.get('column_table_map', {})
    calc_map_ctx = dax_context.get('calc_map', {})
    param_values = dax_context.get('param_values', {})
    measure_names_ctx = dax_context.get('measure_names', set())

    # Pre-compiled aggregation pattern (reused in pre-classification and main loop)
    _agg_pattern = re.compile(
        r'\b(SUM|COUNT|COUNTA|COUNTD|COUNTROWS|AVERAGE|AVG|MIN|MAX|MEDIAN|'
        r'STDEV|STDEVP|VAR|VARP|PERCENTILE|DISTINCTCOUNT|CALCULATE|'
        r'TOTALYTD|SAMEPERIODLASTYEAR|RANKX|SUMX|AVERAGEX|MINX|MAXX|COUNTX|'
        r'CORR|COVAR|COVARP|RUNNING_SUM|RUNNING_AVG|RUNNING_COUNT|RUNNING_MAX|RUNNING_MIN|'
        r'WINDOW_SUM|WINDOW_AVG|WINDOW_MAX|WINDOW_MIN|WINDOW_COUNT|'
        r'WINDOW_MEDIAN|WINDOW_STDEV|WINDOW_STDEVP|WINDOW_VAR|WINDOW_VARP|'
        r'WINDOW_CORR|WINDOW_COVAR|WINDOW_COVARP|WINDOW_PERCENTILE|'
        r'RANK|RANK_UNIQUE|RANK_DENSE|RANK_MODIFIED|RANK_PERCENTILE)\s*\(',
        re.IGNORECASE
    )
    # LOD expressions ({FIXED dim: expr}, {INCLUDE ...}, {EXCLUDE ...}) are
    # aggregation contexts in Tableau — they produce CALCULATE + ALLEXCEPT
    # in DAX.  Detect them separately from the function-call pattern above.
    _lod_pattern = re.compile(
        r'\{\s*(FIXED|INCLUDE|EXCLUDE)\s', re.IGNORECASE
    )

    # --- Pre-classification pass ---
    # Identify which calculations will be calculated columns so that when
    # a calc references another calc-column, we correctly treat it as a
    # column reference (not a measure reference).  Without this, a
    # dimension-role calc that concatenates other calc-columns (e.g.
    # Filière = Nucléaire_vrai & Réseaux_vrai & NSE_vrai) is incorrectly
    # demoted to a measure because the refs appear in calc_map/measure_names.
    prelim_calc_col_captions = set()
    prelim_calc_col_raws = set()
    for _pc in calculations:
        _pc_name = _pc.get('name', '').replace('[', '').replace(']', '')
        _pc_caption = _pc.get('caption', _pc_name)
        _pc_formula = _pc.get('formula', '').strip()
        _pc_role = _pc.get('role', 'measure')
        _pc_is_literal = _pc_formula and '[' not in _pc_formula
        _pc_has_agg = bool(_agg_pattern.search(_pc_formula)) or bool(_lod_pattern.search(_pc_formula))
        # Check for physical column refs (refs not in calc_map/measure_names)
        _pc_refs = re.findall(r'\[([^\]]+)\]', _pc_formula)
        _pc_has_col = False
        for _r in _pc_refs:
            if _r == _pc_caption or _r.startswith('Parameters'):
                continue
            if not (_r in measure_names_ctx or _r in calc_map_ctx.values() or _r in calc_map_ctx):
                _pc_has_col = True
                break
        # A formula without aggregation that has physical column refs is a
        # calculated column regardless of Tableau's role attribute.
        _pc_is_cc = (not _pc_is_literal) and not _pc_has_agg and (
            _pc_role == 'dimension' or _pc_has_col
        )
        if _pc_is_cc:
            prelim_calc_col_captions.add(_pc_caption)
            prelim_calc_col_raws.add(_pc_name)

    # --- Pre-classification fixup ---
    # Iteratively remove calcs from the prelim-calc-col sets when they
    # reference ONLY known calcs/measures that are NOT themselves in the
    # prelim set.  This handles chains like:
    #   Base(has_agg→measure) ← (num)(no_agg→dim) ← OOC(no_agg→dim)
    # where (num) and OOC should cascade to measures.
    _fixup_changed = True
    while _fixup_changed:
        _fixup_changed = False
        for _pc in calculations:
            _pc_name = _pc.get('name', '').replace('[', '').replace(']', '')
            _pc_caption = _pc.get('caption', _pc_name)
            if _pc_name not in prelim_calc_col_raws:
                continue
            _pc_formula = _pc.get('formula', '').strip()
            _pc_refs = re.findall(r'\[([^\]]+)\]', _pc_formula)
            _pc_only_measures = True
            _pc_has_refs = False
            for _r in _pc_refs:
                if _r == _pc_caption or _r.startswith('Parameters'):
                    continue
                _pc_has_refs = True
                _r_known = (_r in measure_names_ctx or
                            _r in calc_map_ctx.values() or
                            _r in calc_map_ctx)
                _r_is_cc = (_r in prelim_calc_col_captions or
                            _r in prelim_calc_col_raws)
                if not (_r_known and not _r_is_cc):
                    _pc_only_measures = False
                    break
            if _pc_only_measures and _pc_has_refs:
                prelim_calc_col_captions.discard(_pc_caption)
                prelim_calc_col_raws.discard(_pc_name)
                measure_names_ctx.add(_pc_caption)
                _fixup_changed = True

    m_calc_steps = []  # Accumulated M Table.AddColumn steps (replaces DAX calc cols)
    dax_only_calc_cols = set()  # Names of calc columns that stayed as DAX (not converted to M)

    # Build column name sets in a single pass — _this_table_columns for
    # same-table ref resolution, _bool_table_columns for type-aware wrapping
    # (MAX/SUM don't support Boolean; need MAXX('T', IF(col, 1, 0))).
    _this_table_columns = set()
    _bool_table_columns = set()
    for _c in columns:
        _cn = _c.get('name', '')
        if _cn:
            _this_table_columns.add(_cn)
            if (_c.get('datatype', '') or '').lower() == 'boolean':
                _bool_table_columns.add(_cn)

    for calc in calculations:
        calc_name = calc.get('name', '').replace('[', '').replace(']', '')
        caption = calc.get('caption', '') or calc_name
        caption = caption.replace('[', '').replace(']', '')
        formula = calc.get('formula', '').strip()
        role = calc.get('role', 'measure')
        datatype = calc.get('datatype', 'string')

        # Skip calculations with no formula (e.g. categorical-bin groups)
        # to avoid generating measures with empty expressions.
        if not formula:
            continue

        # Pure string-literal formulas (e.g. Tableau constant fields like
        # "AIP", "Tout", or a free-text KPI label such as
        # "Average of IF [Won Flag]=""Y"" THEN [Amount] END").  These are
        # valid constant measures — Tableau escapes embedded quotes by
        # doubling them ("") which is exactly DAX's escaping — so they are
        # emitted directly as constant-string measures instead of being
        # silently dropped.  Emitting here (rather than through the normal
        # column-ref classifier) avoids misreading [brackets] that appear
        # *inside* the string literal as real column references.
        _stripped = formula.strip()
        if _stripped.startswith('"') and _stripped.endswith('"') and len(_stripped) > 2:
            # Well-formed iff every interior double-quote is doubled (escaped):
            # removing the doubled pairs must leave no stray quote.
            if _stripped[1:-1].replace('""', '').count('"') == 0:
                _lit_measure = {
                    "name": caption,
                    "expression": _stripped,
                    "formatString": _get_format_string('string'),
                    "displayFolder": _get_display_folder('string', role),
                }
                _orig_lit = calc.get('formula', '')
                if _orig_lit:
                    _lit_measure['_original_formula'] = _orig_lit
                if not any(m.get("name", "").lower() == caption.lower()
                           for m in result_table["measures"]):
                    result_table["measures"].append(_lit_measure)
            # Malformed/unbalanced text metadata: leave out (cannot be a
            # valid DAX literal).  Either way, do not fall through.
            continue

        # Determine if it's a simple literal (parameter) -> measure
        is_literal = formula and '[' not in formula

        # Classify: calculated column or measure
        has_aggregation = bool(_agg_pattern.search(formula)) or bool(_lod_pattern.search(formula))
        refs_in_formula = re.findall(r'\[([^\]]+)\]', formula)
        has_column_refs = False
        references_only_measures = True
        for ref in refs_in_formula:
            if ref == caption:
                continue
            if ref.startswith('Parameters'):
                continue
            # A ref is a "measure/calc ref" ONLY if it's a known calc/param
            # AND it was NOT pre-classified as a calculated column.
            is_known_calc = (ref in measure_names_ctx or
                             ref in calc_map_ctx.values() or
                             ref in calc_map_ctx)
            is_calc_col_ref = (ref in prelim_calc_col_captions or
                               ref in prelim_calc_col_raws)
            is_measure_ref = is_known_calc and not is_calc_col_ref
            if not is_measure_ref:
                has_column_refs = True
                references_only_measures = False
                break

        is_calc_col = (not is_literal) and not has_aggregation and (
            role == 'dimension' or has_column_refs
        )

        # If a calc references ONLY other measures/calcs
        # (no physical columns), it must be a measure — calc columns
        # cannot reference measures in DAX.
        if is_calc_col and not has_column_refs and references_only_measures:
            is_calc_col = False
            # Update prelim sets and measure_names so downstream calcs see
            # correct classification
            prelim_calc_col_captions.discard(caption)
            prelim_calc_col_raws.discard(calc_name)
            measure_names_ctx.add(caption)

        # Security functions must be measures, never calculated columns
        has_security_func = bool(re.search(
            r'\b(USERPRINCIPALNAME|USERNAME|CUSTOMDATA|USERCULTURE)\s*\(',
            dax_context.get('_preview_dax', formula), re.IGNORECASE
        )) or bool(re.search(
            r'\b(USERNAME|FULLNAME|USERDOMAIN|ISMEMBEROF)\s*\(',
            formula, re.IGNORECASE
        ))
        if has_security_func:
            is_calc_col = False

        # Ignore MAKEPOINT (no DAX equivalent)
        if re.search(r'\bMAKEPOINT\b', formula, re.IGNORECASE):
            continue

        dax_formula = convert_tableau_formula_to_dax(
            formula,
            column_name=calc_name,
            table_name=table_name,
            calc_map=dax_context.get('calc_map'),
            param_map=dax_context.get('param_map'),
            column_table_map=column_table_map,
            measure_names=dax_context.get('measure_names'),
            is_calc_column=is_calc_col,
            param_values=param_values,
            calc_datatype=datatype,
            partition_fields=calc.get('table_calc_partitioning'),
            compute_using=dax_context.get('compute_using_map', {}).get(calc_name)
                          or dax_context.get('compute_using_map', {}).get(caption),
            table_columns=_this_table_columns,
            bool_columns=_bool_table_columns,
            validate_output=True,
            fallback_on_invalid=True,
        )

        # Phase 3: record conversion guard fallback to recovery
        if dax_formula and 'TODO: DAX conversion validation failed' in dax_formula:
            logger.warning(
                "DAX conversion guard triggered for '%s' on table '%s'",
                calc_name, table_name,
            )

        if is_calc_col:
            # Post-process: inline literal-value measure references
            for ms in result_table.get("measures", []):
                ms_name = ms.get("name", "")
                ms_expr = ms.get("expression", "").strip()
                if ms_expr and re.match(r'^[\d.]+$|^"[^"]*"$|^true$|^false$|^DATE\(\d+\s*,\s*\d+\s*,\s*\d+\)$|^TIME\(\d+\s*,\s*\d+\s*,\s*\d+\)$', ms_expr, re.IGNORECASE):
                    dax_formula = re.sub(
                        r'\[' + re.escape(ms_name) + r'\]',
                        ms_expr,
                        dax_formula
                    )

            # ── Try to push the calculated column into Power Query M ──
            m_expr = _dax_to_m_expression(dax_formula, table_name)
            # Dependency check: if the M expression references a calc column
            # that stayed as DAX (not converted to M), we must fall back to DAX
            if m_expr is not None:
                # Columns available in M: physical source columns + previously created M steps
                m_available_cols = set(_this_table_columns)
                for step_name, _ in m_calc_steps:
                    # Step names are like '#"Added ColName"' — extract the column name
                    sm = re.match(r'#"Added (.+?)"', step_name)
                    if sm:
                        m_available_cols.add(sm.group(1))
                col_refs = re.findall(r'\[#?"?([^\]"]+)"?\]', m_expr)
                for ref in col_refs:
                    if ref in dax_only_calc_cols:
                        m_expr = None
                        break
                    # M queries can only reference physical columns or prior M step columns.
                    # If a ref doesn't exist in M-available columns, it's likely a
                    # measure or DAX calc column — fall back to DAX.
                    if ref not in m_available_cols:
                        m_expr = None
                        break
            if m_expr is not None:
                m_type = _DAX_TO_M_TYPE.get(
                    map_tableau_to_powerbi_type(datatype), 'type text')
                if m_type in ('Int64.Type', 'type number'):
                    m_expr = _wrap_date_subtraction_in_duration_days(
                        m_expr, columns, col_metadata_map)
                new_step = m_transform_add_column(caption, f'each {m_expr}', m_type)
                # Dedup: replace existing M step for the same column name
                existing_m_idx = None
                for mi, (sn, _) in enumerate(m_calc_steps):
                    sm2 = re.match(r'#"Added (.+?)"', sn)
                    if sm2 and sm2.group(1).lower() == caption.lower():
                        existing_m_idx = mi
                        break
                if existing_m_idx is not None:
                    m_calc_steps[existing_m_idx] = new_step
                else:
                    m_calc_steps.append(new_step)
                bim_calc_col = {
                    "name": caption,
                    "dataType": map_tableau_to_powerbi_type(datatype),
                    "sourceColumn": caption,
                    "summarizeBy": "none",
                }
            else:
                # Fallback: keep as DAX calculated column
                dax_only_calc_cols.add(caption)
                bim_calc_col = {
                    "name": caption,
                    "dataType": map_tableau_to_powerbi_type(datatype),
                    "expression": dax_formula,
                    "summarizeBy": "none",
                    "isCalculated": True,
                }
            if datatype == 'real':
                bim_calc_col["formatString"] = "#,0.00"

            calc_meta = col_metadata_map.get(caption, col_metadata_map.get(calc_name, {}))
            if calc_meta.get('hidden', False):
                bim_calc_col["isHidden"] = True
            if calc_meta.get('description', ''):
                bim_calc_col["description"] = calc_meta['description']
            sr = calc_meta.get('semantic_role', '')
            geo_cat = _map_semantic_role_to_category(sr, caption)
            if geo_cat:
                bim_calc_col["dataCategory"] = geo_cat

            # Dedup: replace existing physical/calc column with same name
            existing_idx = None
            for idx, ec in enumerate(result_table["columns"]):
                if ec.get("name", "").lower() == caption.lower():
                    existing_idx = idx
                    break
            if existing_idx is not None:
                result_table["columns"][existing_idx] = bim_calc_col
            else:
                result_table["columns"].append(bim_calc_col)
            # Track the calc column name so subsequent measures can detect
            # bare column refs at conversion time (Phase 5h).
            _this_table_columns.add(caption)
            if (datatype or '').lower() == 'boolean':
                _bool_table_columns.add(caption)
        else:
            dax_formula = _wrap_bare_ref_expression(
                dax_formula, datatype, result_table.get('name', ''))

            # DAX Measure
            bim_measure = {
                "name": caption,
                "expression": dax_formula,
                "formatString": _get_format_string(datatype),
                "displayFolder": _get_display_folder(datatype, role)
            }
            # Propagate description from Tableau (if extracted)
            calc_desc = calc.get('description', '')
            if calc_desc:
                bim_measure['description'] = calc_desc
            # Store original Tableau formula for auto-description generation
            original_formula = calc.get('formula', '')
            if original_formula:
                bim_measure['_original_formula'] = original_formula
            # Propagate lineage metadata from calculation
            if calc.get('_source_workbooks'):
                bim_measure['_source_workbooks'] = calc['_source_workbooks']
            if calc.get('_merge_action'):
                bim_measure['_merge_action'] = calc['_merge_action']
            # Dedup: skip if measure with same name already exists
            existing_measure = any(
                m.get("name", "").lower() == caption.lower()
                for m in result_table["measures"]
            )
            if not existing_measure:
                result_table["measures"].append(bim_measure)

    _promote_measure_dependent_calc_columns(result_table)
    _unwrap_aggregations_of_measures(result_table)
    _wrap_bare_cross_table_refs(result_table)
    _validate_measures_after_rewrites(result_table)

    # Inject accumulated M steps into the partition (replaces DAX calc cols)
    if m_calc_steps:
        _inject_m_steps_into_partition(result_table, m_calc_steps)

    # Propagate lineage metadata from source table (merge pipeline)
    if table.get('_source_workbooks'):
        result_table['_source_workbooks'] = table['_source_workbooks']
    if table.get('_merge_action'):
        result_table['_merge_action'] = table['_merge_action']

    return result_table
















def _map_semantic_role_to_category(semantic_role, col_name=''):
    """Map a Tableau semantic-role to a Power BI dataCategory."""
    role_map = {
        '[Country].[Name]': 'Country',
        '[Country].[ISO3166_2]': 'Country',
        '[State].[Name]': 'StateOrProvince',
        '[State].[Abbreviation]': 'StateOrProvince',
        '[County].[Name]': 'County',
        '[City].[Name]': 'City',
        '[ZipCode].[Name]': 'PostalCode',
        '[Latitude]': 'Latitude',
        '[Longitude]': 'Longitude',
        '[Geographical].[Latitude]': 'Latitude',
        '[Geographical].[Longitude]': 'Longitude',
        '[Address]': 'Address',
        '[Continent].[Name]': 'Continent',
    }
    if semantic_role in role_map:
        return role_map[semantic_role]

    if not semantic_role:
        name_lower = col_name.lower()
        if 'latitude' in name_lower or name_lower in ('lat', 'lat_upgrade'):
            return 'Latitude'
        if 'longitude' in name_lower or name_lower in ('lon', 'lng', 'long', 'long_upgrade'):
            return 'Longitude'
        if name_lower in ('city', 'ville', 'commune', 'label') and 'code' not in name_lower:
            return 'City'
        if name_lower in ('country', 'pays') or name_lower.startswith('pays/'):
            return 'Country'
        if any(x in name_lower for x in ['region', '\u00e9tat', 'state', 'province', 'd\u00e9partement']):
            return 'StateOrProvince'
        if 'postal' in name_lower or 'zip' in name_lower or 'code_postal' in name_lower:
            return 'PostalCode'

    return None


def _get_display_folder(datatype, role):
    """Determine the display folder based on type and role."""
    if role == 'dimension':
        return 'Dimensions'
    if datatype in ('real', 'integer', 'number'):
        return 'Measures'
    if datatype in ('date', 'datetime'):
        return 'Time Intelligence'
    if datatype == 'boolean':
        return 'Flags'
    return 'Calculations'


# Sets, groups and bins live in tmdl_sets; re-exported here so existing
# imports keep working.
from powerbi_import.tmdl_sets import (  # noqa: E402,F401
    _clean_tableau_field_ref,
    _process_sets_groups_bins,
    _RE_TMDL_DERIVATION_PREFIX,
    _RE_TMDL_TYPE_SUFFIX,
)


# Hierarchies live in tmdl_hierarchies; re-exported here so existing
# imports keep working.
from powerbi_import.tmdl_hierarchies import (  # noqa: E402,F401
    _apply_hierarchies,
    _auto_date_hierarchies,
)


def _rewrite_bare_measure_references(model, old_name, new_name):
    """Update every DAX expression that bare-references a renamed measure.

    When a measure is renamed (e.g. to resolve a name collision), any other
    measure/calculated-column expression using the unqualified ``[OldName]``
    form would otherwise keep pointing at a name that no longer exists,
    producing a Power BI load/eval error ("value cannot be determined").
    Only unqualified references are rewritten — ``Table[OldName]`` /
    ``'Table'[OldName]`` column references are left untouched since they
    refer to a column, not the renamed measure.
    """
    if not old_name or old_name == new_name:
        return
    pattern = re.compile(r"(?<![\w'])\[" + re.escape(old_name) + r"\]")
    for table in model["model"]["tables"]:
        for measure in table.get("measures", []):
            expr = measure.get("expression", "")
            if isinstance(expr, str) and pattern.search(expr):
                measure["expression"] = pattern.sub(f"[{new_name}]", expr)
        for column in table.get("columns", []):
            expr = column.get("expression", "")
            if isinstance(expr, str) and pattern.search(expr):
                column["expression"] = pattern.sub(f"[{new_name}]", expr)


# What-If parameters, calculation groups and field parameters live in
# tmdl_parameters; re-exported here so existing imports keep working.
from powerbi_import.tmdl_parameters import (  # noqa: E402,F401
    _create_calculation_groups,
    _create_field_parameters,
    _create_parameter_tables,
    _parse_switch_branches,
)


# RLS roles live in tmdl_rls; re-exported here so existing imports keep
# working.
from powerbi_import.tmdl_rls import (  # noqa: E402,F401
    _create_rls_roles,
    _unique_role_name,
)


def _get_format_string(datatype):
    """Return the Power BI format string for a given type."""
    format_map = {
        'integer': '0',
        'real': '#,0.00',
        'currency': '$#,0.00',
        'percentage': '0.00%',
        'date': 'Short Date',
        'datetime': 'General Date',
        'boolean': 'True/False'
    }
    return format_map.get(datatype.lower(), '0')


def _convert_tableau_format_to_pbi(tableau_format):
    """Convert a Tableau number format string to Power BI format string.

    Tableau formats:  #,##0.00  |  0.0%  |  $#,##0  |  0.000  |  #,##0
    PBI formats:      #,0.00   |  0.0%  |  $#,0    |  0.000  |  #,0

    Args:
        tableau_format: Tableau format string (from default-format attribute)

    Returns:
        str: Power BI format string, or empty string if no conversion needed
    """
    if not tableau_format:
        return ''

    fmt = tableau_format.strip()

    # Already a PBI-compatible format
    if fmt in ('0', '#,0', '#,0.00', '0.00%', '$#,0.00', 'General Date', 'Short Date'):
        return fmt

    # Percentage formats
    if '%' in fmt:
        # Normalize: Tableau uses 0.0% or 0.00% etc.
        return fmt

    # Currency with symbol
    for symbol in ('$', '€', '£', '¥'):
        if symbol in fmt:
            # Convert Tableau ##0 pattern to PBI #,0 pattern
            cleaned = fmt.replace('##0', '#0').replace('###', '#').replace(',,', ',')
            # Ensure at least one digit placeholder
            if '0' not in cleaned:
                cleaned = cleaned + '0'
            return cleaned

    # Numeric formats
    # Tableau uses #,##0.00 → PBI uses #,0.00
    result = fmt
    # Convert Tableau's #,##0 → #,0 pattern
    result = result.replace('#,##0', '#,0')
    result = result.replace('#,###', '#,#')
    # Handle plain 0 patterns
    if result and result[0] == '0':
        return result  # Already numeric

    return result if result != fmt else fmt




def _create_number_of_records_measure(model, worksheets, main_table_name):
    """Auto-generate a 'Number of Records' COUNTROWS measure.

    Tableau worksheets that use COUNT(*) on ``__tableau_internal_object_id__``
    are extracted with a synthetic field ``Number of Records`` (aggregation=cnt).
    This function creates the corresponding DAX measure on the main table.
    """
    if not worksheets or not main_table_name:
        return

    # Check if any worksheet field uses "Number of Records"
    needs_measure = False
    for ws in worksheets:
        for f in ws.get('fields', []):
            if f.get('name') == 'Number of Records':
                needs_measure = True
                break
        if needs_measure:
            break

    if not needs_measure:
        return

    # Find the main table and add the measure (if not already present)
    for table in model['model']['tables']:
        if table.get('name') == main_table_name:
            existing = {m.get('name') for m in table.get('measures', [])}
            existing_columns = {c.get('name') for c in table.get('columns', [])}
            if 'Number of Records' not in existing and 'Number of Records' not in existing_columns:
                table.setdefault('measures', []).append({
                    'name': 'Number of Records',
                    'expression': "COUNTROWS('" + main_table_name.replace("'", "''") + "')",
                    'displayFolder': 'Measures',
                    'annotations': [
                        {'name': 'MigrationNote',
                         'value': 'Auto-generated from Tableau COUNT(*) on internal object ID.'}
                    ]
                })
            break


def _remove_conflicting_number_of_records_measures(model):
    """Remove 'Number of Records' measures that collide with same-named columns.

    Power BI does not allow a measure and a column with identical names in the
    same table. Some Tableau sources contain both, so we keep the column and
    drop only the conflicting measure.
    """
    for table in model.get('model', {}).get('tables', []):
        columns = {c.get('name') for c in table.get('columns', [])}
        if 'Number of Records' not in columns:
            continue
        measures = table.get('measures', [])
        filtered = [m for m in measures if m.get('name') != 'Number of Records']
        if len(filtered) != len(measures):
            table['measures'] = filtered


def _create_quick_table_calc_measures(model, worksheets, main_table_name, column_table_map):
    """Auto-generate DAX measures for Tableau quick table calculations.
    
    Detects fields with table_calc metadata (pcto, pctd, running_sum, rank, etc.)
    and creates corresponding DAX measures:
    - pcto (% of Total): DIVIDE(SUM([Field]), CALCULATE(SUM([Field]), ALL('Table')))
    - pctd (% Difference): DIVIDE(SUM([Field]) - CALCULATE(SUM([Field]), PREVIOUSDAY(...)), ...)
    - running_sum: CALCULATE(SUM([Field]), FILTER(ALL('Calendar'[Date]), ...))
    - running_avg, running_count, running_min, running_max: similar pattern
    - rank / rank_unique / rank_dense: RANKX(ALL('Table'), SUM([Field]))
    """
    if not worksheets:
        return
    
    # Find the main table to add measures to
    target_table = None
    for t in model["model"]["tables"]:
        if t.get("name") == main_table_name:
            target_table = t
            break
    if not target_table:
        return
    
    existing_measures = {m.get("name", "") for m in target_table.get("measures", [])}
    added = 0
    
    _AGG_MAP = {
        'sum': 'SUM', 'avg': 'AVERAGE', 'count': 'COUNT',
        'min': 'MIN', 'max': 'MAX', 'countd': 'DISTINCTCOUNT',
    }
    
    for ws in worksheets:
        for field in ws.get('fields', []):
            tc_type = field.get('table_calc')
            if not tc_type:
                continue
            
            field_name = field.get('name', '')
            tc_agg = field.get('table_calc_agg', 'sum')
            agg_func = _AGG_MAP.get(tc_agg, 'SUM')
            tbl = column_table_map.get(field_name, main_table_name)
            
            if tc_type == 'pcto':
                measure_name = f"% of Total {field_name}"
                if measure_name not in existing_measures:
                    expr = f"DIVIDE({agg_func}('{tbl}'[{field_name}]), CALCULATE({agg_func}('{tbl}'[{field_name}]), ALL('{tbl}')))"
                    target_table.setdefault("measures", []).append({
                        "name": measure_name,
                        "expression": expr,
                        "formatString": "0.00%",
                        "displayFolder": "Table Calculations"
                    })
                    existing_measures.add(measure_name)
                    added += 1
            
            elif tc_type == 'pctd':
                measure_name = f"% Difference {field_name}"
                if measure_name not in existing_measures:
                    base = f"{agg_func}('{tbl}'[{field_name}])"
                    prev = f"CALCULATE({base}, PREVIOUSDAY('Calendar'[Date]))"
                    expr = f"VAR _Current = {base} VAR _Previous = {prev} RETURN DIVIDE(_Current - _Previous, _Previous)"
                    target_table.setdefault("measures", []).append({
                        "name": measure_name,
                        "expression": expr,
                        "formatString": "0.00%",
                        "displayFolder": "Table Calculations"
                    })
                    existing_measures.add(measure_name)
                    added += 1
            
            elif tc_type.startswith('running_'):
                running_agg = tc_type.replace('running_', '')
                running_func = _AGG_MAP.get(running_agg, 'SUM')
                measure_name = f"Running {running_agg.title()} {field_name}"
                if measure_name not in existing_measures:
                    expr = (f"CALCULATE({running_func}('{tbl}'[{field_name}]), "
                            f"FILTER(ALL('Calendar'[Date]), 'Calendar'[Date] <= MAX('Calendar'[Date])))")
                    target_table.setdefault("measures", []).append({
                        "name": measure_name,
                        "expression": expr,
                        "formatString": "#,0.00",
                        "displayFolder": "Table Calculations"
                    })
                    existing_measures.add(measure_name)
                    added += 1
            
            elif tc_type in ('rank', 'rank_unique', 'rank_dense'):
                dense = ", DENSE" if tc_type == 'rank_dense' else ""
                measure_name = f"Rank {field_name}"
                if measure_name not in existing_measures:
                    expr = f"RANKX(ALL('{tbl}'), {agg_func}('{tbl}'[{field_name}]){dense})"
                    target_table.setdefault("measures", []).append({
                        "name": measure_name,
                        "expression": expr,
                        "formatString": "#,0",
                        "displayFolder": "Table Calculations"
                    })
                    existing_measures.add(measure_name)
                    added += 1
            
            elif tc_type == 'diff':
                measure_name = f"Difference {field_name}"
                if measure_name not in existing_measures:
                    base = f"{agg_func}('{tbl}'[{field_name}])"
                    prev = f"CALCULATE({base}, PREVIOUSDAY('Calendar'[Date]))"
                    expr = f"{base} - {prev}"
                    target_table.setdefault("measures", []).append({
                        "name": measure_name,
                        "expression": expr,
                        "formatString": "#,0.00",
                        "displayFolder": "Table Calculations"
                    })
                    existing_measures.add(measure_name)
                    added += 1
    
    if added:
        print(f"  ✓ {added} quick table calc measures generated")


# Date-table detection and the generated Calendar live in tmdl_dates;
# re-exported here so existing imports keep working.
from powerbi_import.tmdl_dates import (  # noqa: E402,F401
    _add_date_table,
    _DATE_PART_PATTERNS,
    _DATE_TABLE_NAMES,
    _is_date_table,
)


# ════════════════════════════════════════════════════════════════════
#  TMDL FILE WRITERS
# ════════════════════════════════════════════════════════════════════


# TMDL serialization lives in tmdl_writers; re-exported here so existing
# imports of these names keep working.
from powerbi_import.tmdl_writers import (  # noqa: E402
    _generate_column_description,
    _generate_measure_description,
    _generate_table_description,
    _get_display_folder_translations,
    _quote_name,
    _safe_filename,
    _tmdl_datatype,
    _tmdl_summarize,
    _write_column,
    _write_column_flags,
    _write_column_properties,
    _write_culture_tmdl,
    _write_database_tmdl,
    _write_direct_lake_expression,
    _write_expressions_tmdl,
    _write_hierarchy,
    _write_measure,
    _write_model_tmdl,
    _write_multi_language_cultures,
    _write_partition,
    _write_perspectives_tmdl,
    _write_refresh_policy,
    _write_relationships_tmdl,
    _write_roles_tmdl,
    _write_table_tmdl,
    _write_tmdl_files,
    _DISPLAY_FOLDER_TRANSLATIONS,
)








# ════════════════════════════════════════════════════════════════════
#  THEME GENERATION
# ════════════════════════════════════════════════════════════════════

# Default Power BI color palette (used when Tableau has no theme)
_DEFAULT_PBI_COLORS = [
    "#4E79A7", "#F28E2B", "#E15759", "#76B7B2",
    "#59A14F", "#EDC948", "#B07AA1", "#FF9DA7",
    "#9C755F", "#BAB0AC", "#86BCB6", "#8CD17D"
]


def generate_theme_json(theme_data=None):
    """
    Generate a Power BI theme.json from extracted Tableau dashboard theme data.

    Sprint 79: Enhanced with background color, border style, and font mapping.

    Args:
        theme_data: dict with 'colors' (list of hex), 'font_family', 'styles',
                    'background_color', 'border_color', 'border_width'
                    from extract_theme() in extract_tableau_data.py

    Returns:
        dict: Power BI theme definition
    """
    colors = _DEFAULT_PBI_COLORS
    font_family = "Segoe UI"
    background = "#FFFFFF"
    foreground = "#252423"

    # Tableau → web-safe font mapping
    font_map = {
        'Tableau Book': 'Segoe UI',
        'Tableau Light': 'Segoe UI Light',
        'Tableau Medium': 'Segoe UI Semibold',
        'Tableau Bold': 'Segoe UI Bold',
        'Tableau Semibold': 'Segoe UI Semibold',
        'Benton Sans': 'Segoe UI',
        'Benton Sans Book': 'Segoe UI',
    }

    if theme_data:
        t_colors = theme_data.get('colors', [])
        if t_colors:
            # Filter valid hex colors
            valid = [c for c in t_colors if isinstance(c, str) and c.startswith('#')]
            if valid:
                colors = valid[:12]
                # Pad to 12 if fewer
                while len(colors) < 12:
                    colors.append(_DEFAULT_PBI_COLORS[len(colors) % len(_DEFAULT_PBI_COLORS)])
        t_font = theme_data.get('font_family', '')
        if t_font:
            font_family = font_map.get(t_font, t_font)
        # Sprint 79: Background and foreground from styles
        bg = theme_data.get('background_color', '')
        if bg and isinstance(bg, str) and bg.startswith('#'):
            background = bg
        fg = theme_data.get('foreground_color', '')
        if fg and isinstance(fg, str) and fg.startswith('#'):
            foreground = fg

    theme = {
        "name": "Tableau Migration Theme",
        "dataColors": colors,
        "background": background,
        "foreground": foreground,
        "tableAccent": colors[0] if colors else "#4E79A7",
        "textClasses": {
            "callout": {
                "fontSize": 28,
                "fontFace": font_family,
                "color": foreground
            },
            "title": {
                "fontSize": 12,
                "fontFace": font_family,
                "color": foreground
            },
            "header": {
                "fontSize": 12,
                "fontFace": font_family,
                "color": foreground
            },
            "label": {
                "fontSize": 10,
                "fontFace": font_family,
                "color": "#666666"
            }
        },
        "visualStyles": {
            "*": {
                "*": {
                    "*": [{
                        "fontFamily": font_family,
                        "wordWrap": True
                    }]
                }
            }
        }
    }

    # Sprint 79: Border styling
    if theme_data:
        border_color = theme_data.get('border_color', '')
        border_width = theme_data.get('border_width', 0)
        if border_color and isinstance(border_color, str) and border_color.startswith('#'):
            theme["visualStyles"]["*"]["*"]["border"] = [{
                "show": True,
                "color": border_color,
                "width": border_width if border_width else 1,
            }]

    return theme










# ── Display folder translations (built-in) ──────────────────────────────────















# --- Description auto-generation for Copilot/Q&A readiness ---





















# Incremental refresh policy lives in tmdl_refresh_policy; re-exported
# here so existing imports of these names keep working.
from powerbi_import.tmdl_refresh_policy import (  # noqa: E402
    detect_refresh_policy,
    _detect_incremental_refresh_tables,
    _pick_best_date_column,
    _generate_refresh_policy,
    _inject_range_filter_m,
    _generate_incremental_m_parameters,
    apply_incremental_refresh,
    _INCREMENTAL_CONNECTORS,
    _DATE_TYPE_KEYWORDS,
    _DATE_COL_KEYWORDS,
)






