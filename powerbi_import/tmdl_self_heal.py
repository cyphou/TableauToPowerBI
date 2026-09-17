"""Pre-write TMDL self-healing.

Stage 1 of the healing model: validates and repairs the in-memory semantic
model before any TMDL file is written. Extracted from tmdl_generator so the
healing surface has a single owner (@healing); tmdl_generator re-exports
these names for backward compatibility.
"""

import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))
from m_query_builder import wrap_source_with_try_otherwise  # noqa: E402

logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════════
#  SELF-HEALING — SEMANTIC MODEL VALIDATION & REPAIR
# ════════════════════════════════════════════════════════════════════

def _validate_m_partitions(model, recovery=None):
    """Parse every M partition in the model; record issues to recovery.

    Sprint 129.2 generation gate. Non-blocking: issues are logged but do
    not prevent the migration from completing — the .pbip still ships,
    but operators get a per-table audit of any M that may fail to
    refresh in Power BI Desktop or the Service.

    Sprint 131.2: each partition outcome is also recorded to the
    process-wide TelemetryCollector singleton (if telemetry enabled)
    via ``record_validation('m', status, issue_category)``.

    Returns:
        int: total count of M partitions that produced at least one
        validation issue.
    """
    try:
        from powerbi_import.m_validator import validate_m_query
    except Exception:
        return 0

    # Best-effort telemetry hook — never block on telemetry errors.
    telemetry = None
    try:
        from powerbi_import import telemetry as _tel_mod
        telemetry = getattr(_tel_mod, '_GLOBAL_COLLECTOR', None)
    except Exception:
        telemetry = None

    failing = 0
    tables = model.get('model', {}).get('tables', []) or []
    for table in tables:
        tname = table.get('name', '') or '<unnamed>'
        for part in table.get('partitions', []) or []:
            source = part.get('source', {}) or {}
            if source.get('type') != 'm':
                continue
            expr = source.get('expression', '') or ''
            if not expr.strip():
                continue
            try:
                issues = validate_m_query(expr)
            except Exception as exc:  # validator must never block generation
                issues = [f'm_validator raised: {exc!r}']
            if not issues:
                if telemetry is not None:
                    try:
                        telemetry.record_validation('m', 'pass')
                    except Exception:
                        pass
                continue
            failing += 1
            issue_cat = _categorize_m_issue(issues[0])
            if telemetry is not None:
                try:
                    telemetry.record_validation('m', 'fail', issue_cat)
                except Exception:
                    pass
            if recovery is not None:
                recovery.record(
                    category='m_query',
                    repair_type='validation_warning',
                    description=f"M partition '{part.get('name','')}' on table "
                                f"'{tname}' has {len(issues)} parse issue(s)",
                    action='; '.join(issues[:5]),
                    severity='warning',
                    item_name=f'{tname}/{part.get("name","")}',
                )
    return failing


def _categorize_m_issue(issue_msg):
    """Map a validator issue string to a coarse category for telemetry."""
    if not issue_msg:
        return 'unknown'
    s = issue_msg.lower()
    if 'paren' in s or 'bracket' in s or 'brace' in s:
        return 'bracket_balance'
    if 'string' in s or 'quote' in s:
        return 'string_literal'
    if 'let' in s or 'in' in s:
        return 'let_in'
    if 'comma' in s:
        return 'trailing_comma'
    if 'identifier' in s:
        return 'quoted_identifier'
    return 'other'


def _self_heal_model(model, recovery=None):
    """Run post-generation semantic validation and auto-repair.

    Checks for common issues that would prevent the .pbip from opening
    in Power BI Desktop, and applies corrective strategies:

      1. Duplicate table names → auto-suffix with _2, _3, ...
      2. Broken column references in measures → hide measure + MigrationNote
      3. Orphan measures (table missing) → reassign to first available table
      4. Empty table names → skip
      5. Circular relationships → log deactivated
      6. Bare column refs in measures → wrap with MAX()
      7. M partitions without try/otherwise → wrap for error handling
      8. Data type / formatString mismatch → fix dataType
      9. Duplicate column names → auto-suffix with _2, _3, ...
      10. Tables with zero columns → add placeholder or remove
      11. Missing relationship endpoints → remove broken relationships
      12. Measures with empty expressions → remove
      13. Cross-table DAX broken refs → hide measure + MigrationNote

    Args:
        model: Complete semantic model dict
        recovery: Optional RecoveryReport instance for logging repairs

    Returns:
        int: Number of repairs applied
    """
    repairs = 0
    tables = model.get('model', {}).get('tables', [])
    relationships = model.get('model', {}).get('relationships', [])

    # Build lookup of known tables and their columns
    table_names = set()
    table_columns = {}
    for t in tables:
        tname = t.get('name', '')
        if tname:
            table_names.add(tname)
            table_columns[tname] = {c.get('name', '') for c in t.get('columns', [])}

    # 1. Deduplicate table names
    seen_names = {}
    for t in tables:
        tname = t.get('name', '')
        if not tname:
            continue
        if tname in seen_names:
            suffix = 2
            new_name = f"{tname}_{suffix}"
            while new_name in seen_names or new_name in table_names:
                suffix += 1
                new_name = f"{tname}_{suffix}"
            old_name = tname
            t['name'] = new_name
            table_names.add(new_name)
            table_columns[new_name] = table_columns.pop(old_name, set())
            seen_names[new_name] = t
            # Rewrite relationship references
            for rel in relationships:
                if rel.get('fromTable') == old_name:
                    rel['fromTable'] = new_name
                if rel.get('toTable') == old_name:
                    rel['toTable'] = new_name
            repairs += 1
            print(f"  ⚕ Self-heal: Renamed duplicate table '{old_name}' → '{new_name}'")
            if recovery:
                recovery.record('tmdl', 'duplicate_table',
                                item_name=old_name,
                                description=f"Duplicate table name '{old_name}'",
                                action=f"Renamed to '{new_name}'",
                                severity='warning')
        else:
            seen_names[tname] = t

    # 2. Validate measure column references
    measure_names_in_model = set()
    for t in tables:
        for m in t.get('measures', []):
            measure_names_in_model.add(m.get('name', ''))

    all_columns = set()
    for cols in table_columns.values():
        all_columns.update(cols)

    for t in tables:
        tname = t.get('name', '')
        for measure in t.get('measures', []):
            expr = measure.get('expression', '')
            if not expr:
                continue

            # Self-heal: if a measure references missing fields that should be
            # columns, materialize hidden placeholder columns so the model
            # remains loadable in PBI Desktop.
            qualified_refs = re.findall(r"'((?:[^']|'')+)'\[([^\]]+)\]", expr)
            # Accept Unicode identifiers for unquoted table names (e.g. Équipe[Montant]).
            bare_qualified_refs = re.findall(r"\b([^\W\d]\w*)\[([^\]]+)\]", expr)
            foreign_qualified_cols = set()
            unresolved_columns = []
            for q_table, q_col in qualified_refs:
                q_table = q_table.replace("''", "'")
                if q_col in table_columns.get(tname, set()):
                    continue
                if q_col in measure_names_in_model:
                    continue

                if q_table != tname:
                    # Do not synthesize local columns for references that
                    # clearly target another table.
                    foreign_qualified_cols.add(q_col)
                    continue
                unresolved_columns.append(q_col)

            for q_table, q_col in bare_qualified_refs:
                if q_col in table_columns.get(tname, set()):
                    continue
                if q_col in measure_names_in_model:
                    continue

                if q_table != tname:
                    foreign_qualified_cols.add(q_col)
                    continue
                unresolved_columns.append(q_col)

            refs = re.findall(r'\[([^\]]+)\]', expr)
            for ref in refs:
                if ref in table_columns.get(tname, set()):
                    continue
                if ref in measure_names_in_model:
                    continue
                if ref.upper() in ('VALUE', 'FORMAT', 'YEAR', 'MONTH', 'DAY',
                                   'HOUR', 'MINUTE', 'SECOND', 'DATE'):
                    continue
                if ref in foreign_qualified_cols:
                    continue
                unresolved_columns.append(ref)

            if unresolved_columns:
                existing_cols = {c.get('name', '') for c in t.get('columns', []) if c.get('name')}
                created = []
                for missing_col in sorted(set(unresolved_columns)):
                    if missing_col in existing_cols:
                        continue
                    t.setdefault('columns', []).append({
                        'name': missing_col,
                        'dataType': 'string',
                        'isHidden': True,
                        'description': (
                            'Self-heal placeholder column created from '
                            f"measure reference [{missing_col}]"
                        ),
                        'annotations': [{
                            'name': 'MigrationNote',
                            'value': (
                                'Self-heal: placeholder column created to '
                                f"satisfy missing qualified reference '{tname}'[{missing_col}]."
                            ),
                        }],
                    })
                    table_columns.setdefault(tname, set()).add(missing_col)
                    all_columns.add(missing_col)
                    existing_cols.add(missing_col)
                    created.append(missing_col)
                    repairs += 1

                if created:
                    mname = measure.get('name', '?')
                    print(
                        f"  ⚕ Self-heal: Added {len(created)} placeholder column(s) "
                        f"to '{tname}' for measure '{mname}'"
                    )
                    if recovery:
                        recovery.record(
                            'tmdl', 'placeholder_column_ref',
                            item_name=mname,
                            description=(
                                'Missing qualified references in measure: '
                                + ', '.join(f"'{tname}'[{c}]" for c in created)
                            ),
                            action=(
                                'Created hidden placeholder column(s): '
                                + ', '.join(f'[{c}]' for c in created)
                            ),
                            severity='warning',
                            follow_up='Replace placeholders with actual source columns.',
                        )

            # Check for references to columns using [ColumnName] pattern
            broken = False
            for ref in refs:
                # Skip if it's a known measure or known column
                if ref in measure_names_in_model or ref in all_columns:
                    continue
                # Skip DAX keywords/functions
                if ref.upper() in ('VALUE', 'FORMAT', 'YEAR', 'MONTH', 'DAY',
                                   'HOUR', 'MINUTE', 'SECOND', 'DATE'):
                    continue
                # Skip refs that are explicitly qualified to another table.
                if ref in foreign_qualified_cols:
                    continue
                # This reference doesn't resolve — mark as broken
                broken = True
                break

            if broken:
                measure['isHidden'] = True
                measure.setdefault('annotations', []).append({
                    'name': 'MigrationNote',
                    'value': f'Self-heal: measure contains unresolved column reference [{ref}]. Review and fix manually.'
                })
                repairs += 1
                mname = measure.get('name', '?')
                print(f"  ⚕ Self-heal: Hidden measure '{mname}' (broken ref [{ref}])")
                if recovery:
                    recovery.record('tmdl', 'broken_column_ref',
                                    item_name=mname,
                                    description=f"Measure references non-existent column [{ref}]",
                                    action="Measure hidden with MigrationNote",
                                    severity='warning',
                                    follow_up=f"Fix column reference [{ref}] in measure '{mname}'")

    # 2b. Validate calculated column references — add placeholders for missing refs
    for t in tables:
        tname = t.get('name', '')
        existing_cols = {c.get('name', '') for c in t.get('columns', []) if c.get('name')}
        created_for_cc = []
        for col in t.get('columns', []):
            expr = col.get('expression', '')
            if not expr:
                continue
            # Find same-table qualified references in calc column expressions
            qualified_refs = re.findall(r"'((?:[^']|'')+)'\[([^\]]+)\]", expr)
            for q_table, q_col in qualified_refs:
                q_table = q_table.replace("''", "'")
                if q_table != tname:
                    continue
                if q_col in existing_cols or q_col in measure_names_in_model:
                    continue
                # Also check all_columns (might be known elsewhere)
                if q_col in existing_cols:
                    continue
                # Add placeholder
                t.setdefault('columns', []).append({
                    'name': q_col,
                    'dataType': 'string',
                    'isHidden': True,
                    'description': (
                        'Self-heal placeholder column created from '
                        f"calculated column reference [{q_col}]"
                    ),
                    'annotations': [{
                        'name': 'MigrationNote',
                        'value': (
                            'Self-heal: placeholder column created to '
                            f"satisfy missing reference '{tname}'[{q_col}] "
                            f"in calculated column '{col.get('name', '?')}'."
                        ),
                    }],
                })
                table_columns.setdefault(tname, set()).add(q_col)
                all_columns.add(q_col)
                existing_cols.add(q_col)
                created_for_cc.append(q_col)
                repairs += 1

            # Also check bare [ColumnName] references (not qualified)
            bare_refs = re.findall(r'\[([^\]]+)\]', expr)
            for ref in bare_refs:
                if ref in existing_cols or ref in measure_names_in_model:
                    continue
                if ref.upper() in ('VALUE', 'FORMAT', 'YEAR', 'MONTH', 'DAY',
                                   'HOUR', 'MINUTE', 'SECOND', 'DATE'):
                    continue
                # Add placeholder
                t.setdefault('columns', []).append({
                    'name': ref,
                    'dataType': 'string',
                    'isHidden': True,
                    'description': (
                        'Self-heal placeholder column created from '
                        f"calculated column reference [{ref}]"
                    ),
                    'annotations': [{
                        'name': 'MigrationNote',
                        'value': (
                            'Self-heal: placeholder column created to '
                            f"satisfy missing reference [{ref}] "
                            f"in calculated column '{col.get('name', '?')}'."
                        ),
                    }],
                })
                table_columns.setdefault(tname, set()).add(ref)
                all_columns.add(ref)
                existing_cols.add(ref)
                created_for_cc.append(ref)
                repairs += 1

        if created_for_cc:
            print(
                f"  ⚕ Self-heal: Added {len(created_for_cc)} placeholder column(s) "
                f"to '{tname}' for calculated columns"
            )
            if recovery:
                recovery.record(
                    'tmdl', 'placeholder_column_calc_col',
                    item_name=tname,
                    description=(
                        'Missing references in calculated columns: '
                        + ', '.join(f"[{c}]" for c in created_for_cc)
                    ),
                    action=(
                        'Created hidden placeholder column(s): '
                        + ', '.join(f'[{c}]' for c in created_for_cc)
                    ),
                    severity='warning',
                    follow_up='Replace placeholders with actual source columns.',
                )

    # 3. Orphan measures — measures on tables that got removed
    #    (shouldn't normally happen, but defensive)
    main_table = tables[0] if tables else None
    for t in list(tables):
        tname = t.get('name', '')
        if not tname and t.get('measures'):
            # Table has no name — move measures to main table
            if main_table and main_table is not t:
                for m in t.get('measures', []):
                    m.setdefault('annotations', []).append({
                        'name': 'MigrationNote',
                        'value': f'Self-heal: orphan measure reassigned from unnamed table.'
                    })
                    main_table.setdefault('measures', []).append(m)
                    repairs += 1
                    print(f"  ⚕ Self-heal: Reassigned orphan measure '{m.get('name', '?')}' to '{main_table.get('name', '')}'")
                    if recovery:
                        recovery.record('tmdl', 'orphan_measure',
                                        item_name=m.get('name', '?'),
                                        description="Measure on unnamed table",
                                        action=f"Reassigned to '{main_table.get('name', '')}'",
                                        severity='info')
                t['measures'] = []

    # 4. Remove empty-name tables (defensive)
    original_count = len(tables)
    model['model']['tables'] = [t for t in tables if t.get('name', '').strip()]
    removed = original_count - len(model['model']['tables'])
    if removed:
        repairs += removed
        print(f"  ⚕ Self-heal: Removed {removed} unnamed table(s)")
        if recovery:
            recovery.record('tmdl', 'empty_table_name',
                            description=f"Removed {removed} table(s) with empty names",
                            action="Tables removed from model",
                            severity='warning')

    # 5. Circular relationship detection already handled by _deactivate_ambiguous_paths
    #    but log to recovery report if any were deactivated
    deactivated = [r for r in relationships if r.get('isActive') == False]
    for rel in deactivated:
        if recovery:
            desc = (f"{rel.get('fromTable','')}.{rel.get('fromColumn','')} → "
                    f"{rel.get('toTable','')}.{rel.get('toColumn','')}")
            recovery.record('relationship', 'deactivated_ambiguous',
                            item_name=desc,
                            description="Relationship creates ambiguous path (cycle)",
                            action="Deactivated to break cycle",
                            severity='info')

    # 6. Wrap bare column references in measures with MAX()
    #    When a measure references a calculated column from the same table
    #    without aggregation (e.g. inside IF, SWITCH), PBI errors with
    #    "single value cannot be determined".  Wrapping in MAX() is safe
    #    because LOD-derived calc columns have one value per filter context.
    _HEAL_AGG_RE = re.compile(
        r'\b(?:SUM|AVERAGE|MIN|MAX|COUNT|COUNTA|COUNTBLANK|DISTINCTCOUNT|'
        r'SUMX|AVERAGEX|MINX|MAXX|COUNTX|COUNTAX|CALCULATE|FILTER|'
        r'LOOKUPVALUE|RELATED|RANKX|PERCENTILE|MEDIAN|STDEV|VAR|'
        r'ALLEXCEPT|REMOVEFILTERS|ALL|VALUES|HASONEVALUE|SELECTEDVALUE|'
        r'EARLIER|EARLIEST|CONCATENATEX|TOPN|ADDCOLUMNS|SUMMARIZE|'
        r'GENERATE|GENERATEALL|TREATAS|USERELATIONSHIP|CROSSFILTER|'
        r'TOTALYTD|TOTALQTD|TOTALMTD|DATESYTD|DATESMTD|DATESQTD|'
        r'DATEADD|DATESBETWEEN|DATESINPERIOD|SAMEPERIODLASTYEAR|'
        r'PREVIOUSDAY|PREVIOUSMONTH|PREVIOUSQUARTER|PREVIOUSYEAR|'
        r'NEXTDAY|NEXTMONTH|NEXTQUARTER|NEXTYEAR|PARALLELPERIOD|'
        r'STARTOFMONTH|STARTOFQUARTER|STARTOFYEAR|'
        r'ENDOFMONTH|ENDOFQUARTER|ENDOFYEAR|'
        r'FIRSTDATE|LASTDATE|FIRSTNONBLANK|LASTNONBLANK|'
        r'CLOSINGBALANCEMONTH|CLOSINGBALANCEQUARTER|CLOSINGBALANCEYEAR|'
        r'OPENINGBALANCEMONTH|OPENINGBALANCEQUARTER|OPENINGBALANCEYEAR|'
        r'COUNTROWS|DIVIDE|DISTINCTCOUNTNOBLANK|COMBINEVALUES|CONTAINS|'
        r'PATH|PATHITEM|SELECTCOLUMNS)\s*\(',
        re.IGNORECASE
    )
    _TABLE_COL_RE = re.compile(r"'((?:[^']|'')+)'\[([^\]]+)\]")
    
    # Phase 11: Enhanced many-to-many detection via column name similarity
    #           Improves on Phase 10 by scoring column overlap (Jaccard)
    def _similarity_score(from_cols, to_cols):
        """Compute Jaccard similarity between column sets."""
        from_set = {c.lower() for c in from_cols}
        to_set = {c.lower() for c in to_cols}
        if not from_set or not to_set:
            return 0.0
        intersection = len(from_set & to_set)
        union = len(from_set | to_set)
        return intersection / union if union > 0 else 0.0
    
    # Upgrade manyToOne to manyToMany if overlap is high (>80%) and table sizes are similar
    for rel in relationships:
        if rel.get('cardinality') != 'manyToOne':
            continue
        from_table_name = rel.get('fromTable')
        to_table_name = rel.get('toTable')
        from_tbl = next((t for t in tables if t.get('name') == from_table_name), None)
        to_tbl = next((t for t in tables if t.get('name') == to_table_name), None)
        if not from_tbl or not to_tbl:
            continue
        
        from_cols = {c.get('name', '') for c in from_tbl.get('columns', []) if c.get('name')}
        to_cols = {c.get('name', '') for c in to_tbl.get('columns', []) if c.get('name')}
        similarity = _similarity_score(from_cols, to_cols)
        from_size = len(from_cols)
        to_size = len(to_cols)
        size_ratio = min(to_size, from_size) / max(to_size, from_size) if max(to_size, from_size) > 0 else 0.0
        
        # If >80% column overlap and tables have similar width, likely peer tables (many-to-many)
        if similarity > 0.80 and size_ratio > 0.70:
            rel['cardinality'] = 'manyToMany'
            print(f"  ⚕ Phase 11: Upgraded '{from_table_name}' → '{to_table_name}' to manyToMany (overlap={similarity:.0%})")

    for t in model.get('model', {}).get('tables', []):
        tname = t.get('name', '')
        col_names = {c.get('name', '') for c in t.get('columns', []) if c.get('name')}
        local_measures = {m.get('name', '') for m in t.get('measures', []) if m.get('name')}
        # Boolean columns need special wrapping: MAX() doesn't support
        # Boolean type in DAX.  Use MAX(IF(col, 1, 0)) instead.
        bool_cols = {c.get('name', '') for c in t.get('columns', [])
                     if (c.get('dataType', '') or '').lower() == 'boolean'
                     and c.get('name')}

        for measure in t.get('measures', []):
            expr = measure.get('expression', '')
            if not expr:
                continue
            # Find all 'Table'[Column] references in the expression
            refs = list(_TABLE_COL_RE.finditer(expr))
            if not refs:
                continue

            # Process refs in reverse order to preserve positions
            new_expr = expr
            wrapped_any = False
            for ref_match in reversed(refs):
                ref_table = ref_match.group(1).replace("''", "'")
                ref_col = ref_match.group(2)

                # Only wrap refs to columns (not measures) in same table
                if ref_table != tname:
                    continue
                if ref_col in local_measures:
                    continue
                if ref_col not in col_names:
                    continue

                # Backward paren walk: check if ANY enclosing function is an
                # aggregation/iterator/time-intelligence function.
                # Tracks paren depth to correctly skip sibling clauses.
                # E.g. SUMX('T', IF('T'[Col]>0, ...)) — IF is nearest paren
                # but SUMX provides row context at a higher nesting level.
                prefix = new_expr[:ref_match.start()]
                inside_agg = False
                depth = 0
                for i in range(len(prefix) - 1, -1, -1):
                    if prefix[i] == ')':
                        depth += 1
                    elif prefix[i] == '(':
                        if depth > 0:
                            depth -= 1
                        else:
                            # Found an unclosed paren — extract the
                            # function name immediately before '(' and
                            # check ONLY that name (not the entire prefix).
                            func_prefix = prefix[:i].rstrip()
                            fname_m = re.search(r'(\w+)\s*$', func_prefix)
                            if fname_m and _HEAL_AGG_RE.search(fname_m.group(1) + '('):
                                inside_agg = True
                                break
                if inside_agg:
                    continue

                # Wrap: 'Table'[Col] → MAX('Table'[Col])
                # For Boolean columns, MAX(IF(col, 1, 0)) is invalid because
                # MAX with a single arg needs a column reference, not an
                # expression.  Use MAXX('Table', IF(col, 1, 0)) instead.
                ref_text = ref_match.group(0)
                tbl_esc = tname.replace("'", "''")
                if ref_col in bool_cols:
                    new_expr = (new_expr[:ref_match.start()] +
                                f"MAXX('{tbl_esc}', IF({ref_text}, 1, 0))" +
                                new_expr[ref_match.end():])
                else:
                    new_expr = (new_expr[:ref_match.start()] +
                                f'MAX({ref_text})' +
                                new_expr[ref_match.end():])
                wrapped_any = True

            if wrapped_any:
                measure['expression'] = new_expr
                repairs += 1
                mname = measure.get('name', '?')
                print(f"  ⚕ Self-heal: Wrapped bare column refs in measure '{mname}' with MAX()")
                if recovery:
                    recovery.record('tmdl', 'bare_column_ref_in_measure',
                                    item_name=mname,
                                    description=f"Measure references column without aggregation",
                                    action="Wrapped bare column references in MAX()",
                                    severity='info',
                                    follow_up=f"Review measure '{mname}' — MAX() may not be the best aggregation")

    # 7. M query self-repair — ensure all M partitions have try/otherwise wrapping
    for t in model.get('model', {}).get('tables', []):
        tname = t.get('name', '')
        for part in t.get('partitions', []):
            src = part.get('source', {})
            if src.get('type') != 'm':
                continue
            m_expr = src.get('expression', '')
            if not m_expr or 'try' in m_expr:
                continue  # Already wrapped or empty
            # Only wrap partitions that have a 'let ... in' structure
            if 'let' not in m_expr.lower():
                continue
            col_names = [c.get('name', '') for c in t.get('columns', []) if c.get('name')]
            wrapped = wrap_source_with_try_otherwise(m_expr, col_names)
            if wrapped != m_expr:
                src['expression'] = wrapped
                repairs += 1
                if recovery:
                    recovery.record('m_query', 'try_otherwise_wrap',
                                    item_name=tname,
                                    description=f"M partition for '{tname}' lacks error handling",
                                    action="Wrapped Source with try...otherwise fallback",
                                    severity='info')

    # 8. Data type / formatString consistency
    #    A numeric formatString on a String column causes PBI Desktop to
    #    report "Missing_References".  Fix by changing dataType to Double.
    _NUMERIC_FMT_RE = re.compile(r'[#0,.]')  # digits/decimal in format
    for t in model.get('model', {}).get('tables', []):
        tname = t.get('name', '')
        for col in t.get('columns', []):
            cname = col.get('name', '')
            dt = (col.get('dataType') or '').lower()
            fmt = col.get('formatString', '')
            if dt == 'string' and fmt and _NUMERIC_FMT_RE.search(fmt):
                # Numeric format on a string column — fix type
                col['dataType'] = 'Double'
                col['summarizeBy'] = 'sum'
                repairs += 1
                print(f"  \u2695 Self-heal: Fixed dataType for '{tname}'.'{cname}' "
                      f"String \u2192 Double (formatString '{fmt}')")
                if recovery:
                    recovery.record('tmdl', 'datatype_format_mismatch',
                                    item_name=f'{tname}.{cname}',
                                    description=f"Column '{cname}' has dataType String "
                                                f"but numeric formatString '{fmt}'",
                                    action="Changed dataType to Double",
                                    severity='warning')

    # 9. Duplicate column names within a table
    #    PBI Desktop crashes when two columns share the same name.
    for t in model.get('model', {}).get('tables', []):
        tname = t.get('name', '')
        seen_cols = {}
        for col in t.get('columns', []):
            cname = col.get('name', '')
            if not cname:
                continue
            if cname in seen_cols:
                suffix = 2
                new_name = f"{cname}_{suffix}"
                existing = {c.get('name', '') for c in t.get('columns', [])}
                while new_name in existing:
                    suffix += 1
                    new_name = f"{cname}_{suffix}"
                col['name'] = new_name
                repairs += 1
                print(f"  \u2695 Self-heal: Renamed duplicate column '{tname}'.'{cname}' \u2192 '{new_name}'")
                if recovery:
                    recovery.record('tmdl', 'duplicate_column',
                                    item_name=f'{tname}.{cname}',
                                    description=f"Duplicate column name '{cname}' in table '{tname}'",
                                    action=f"Renamed to '{new_name}'",
                                    severity='warning')
            else:
                seen_cols[cname] = col

    # 10. Tables with zero columns (PBI Desktop can't load them)
    tables_after = model.get('model', {}).get('tables', [])
    empty_tables = [t for t in tables_after
                    if not t.get('columns') and t.get('name', '').strip()]
    for t in empty_tables:
        tname = t.get('name', '')
        # Keep the table only if it has measures — add a placeholder column
        if t.get('measures'):
            t['columns'] = [{
                'name': '_Placeholder',
                'dataType': 'String',
                'sourceColumn': '_Placeholder',
                'summarizeBy': 'none',
                'isHidden': True,
            }]
            repairs += 1
            print(f"  \u2695 Self-heal: Added placeholder column to empty table '{tname}'")
            if recovery:
                recovery.record('tmdl', 'empty_table_columns',
                                item_name=tname,
                                description=f"Table '{tname}' has measures but no columns",
                                action="Added hidden _Placeholder column",
                                severity='info')
        else:
            # No columns AND no measures — remove entirely
            tables_after.remove(t)
            # Clean up relationships referencing removed table
            rels = model.get('model', {}).get('relationships', [])
            model['model']['relationships'] = [
                r for r in rels
                if r.get('fromTable') != tname and r.get('toTable') != tname
            ]
            repairs += 1
            print(f"  \u2695 Self-heal: Removed empty table '{tname}' (no columns, no measures)")
            if recovery:
                recovery.record('tmdl', 'empty_table_removed',
                                item_name=tname,
                                description=f"Table '{tname}' has no columns and no measures",
                                action="Removed from model",
                                severity='warning')

    # 11. Missing relationship endpoints
    #     Remove relationships referencing non-existent tables or columns.
    current_tables = {t.get('name', ''): t
                      for t in model.get('model', {}).get('tables', [])
                      if t.get('name', '')}
    valid_rels = []
    for rel in model.get('model', {}).get('relationships', []):
        ft = rel.get('fromTable', '')
        tt = rel.get('toTable', '')
        fc = rel.get('fromColumn', '')
        tc = rel.get('toColumn', '')
        from_t = current_tables.get(ft)
        to_t = current_tables.get(tt)
        if not from_t or not to_t:
            missing = ft if not from_t else tt
            repairs += 1
            desc = f"{ft}[{fc}] \u2192 {tt}[{tc}]"
            print(f"  \u2695 Self-heal: Removed relationship {desc} (table '{missing}' not found)")
            if recovery:
                recovery.record('relationship', 'missing_table',
                                item_name=desc,
                                description=f"Relationship references non-existent table '{missing}'",
                                action="Relationship removed",
                                severity='warning')
            continue
        from_cols = {c.get('name', '') for c in from_t.get('columns', [])}
        to_cols = {c.get('name', '') for c in to_t.get('columns', [])}
        if fc and fc not in from_cols:
            repairs += 1
            desc = f"{ft}[{fc}] \u2192 {tt}[{tc}]"
            print(f"  \u2695 Self-heal: Removed relationship {desc} (column '{fc}' not in '{ft}')")
            if recovery:
                recovery.record('relationship', 'missing_column',
                                item_name=desc,
                                description=f"Relationship column '{fc}' not found in table '{ft}'",
                                action="Relationship removed",
                                severity='warning')
            continue
        if tc and tc not in to_cols:
            repairs += 1
            desc = f"{ft}[{fc}] \u2192 {tt}[{tc}]"
            print(f"  \u2695 Self-heal: Removed relationship {desc} (column '{tc}' not in '{tt}')")
            if recovery:
                recovery.record('relationship', 'missing_column',
                                item_name=desc,
                                description=f"Relationship column '{tc}' not found in table '{tt}'",
                                action="Relationship removed",
                                severity='warning')
            continue
        valid_rels.append(rel)
    model['model']['relationships'] = valid_rels

    # 12. Measures with empty expressions
    for t in model.get('model', {}).get('tables', []):
        tname = t.get('name', '')
        remaining_measures = []
        for m in t.get('measures', []):
            expr = (m.get('expression', '') or '').strip()
            mname = m.get('name', '?')
            if not expr:
                repairs += 1
                print(f"  \u2695 Self-heal: Removed empty measure '{mname}' from '{tname}'")
                if recovery:
                    recovery.record('tmdl', 'empty_measure',
                                    item_name=mname,
                                    description=f"Measure '{mname}' in '{tname}' has empty expression",
                                    action="Measure removed",
                                    severity='warning')
                continue
            remaining_measures.append(m)
        t['measures'] = remaining_measures

    # 13. Cross-table DAX references — 'Table'[Column] where table or
    #     column doesn't exist.  Hide the measure and annotate.
    all_table_fields = {}
    for t in model.get('model', {}).get('tables', []):
        tname = t.get('name', '')
        if not tname:
            continue
        fields = {c.get('name', '') for c in t.get('columns', []) if c.get('name')}
        fields |= {m.get('name', '') for m in t.get('measures', []) if m.get('name')}
        all_table_fields[tname] = fields

    for t in model.get('model', {}).get('tables', []):
        for measure in t.get('measures', []):
            if measure.get('isHidden'):
                continue  # Already handled
            expr = measure.get('expression', '')
            if not expr:
                continue
            for ref_match in _TABLE_COL_RE.finditer(expr):
                ref_table = ref_match.group(1).replace("''", "'")
                ref_col = ref_match.group(2)
                if ref_table not in all_table_fields:
                    measure['isHidden'] = True
                    measure.setdefault('annotations', []).append({
                        'name': 'MigrationNote',
                        'value': f"Self-heal: references non-existent table '{ref_table}'. Review and fix."
                    })
                    repairs += 1
                    mname = measure.get('name', '?')
                    print(f"  \u2695 Self-heal: Hidden measure '{mname}' (unknown table '{ref_table}')")
                    if recovery:
                        recovery.record('tmdl', 'cross_table_broken_ref',
                                        item_name=mname,
                                        description=f"Measure references non-existent table '{ref_table}'",
                                        action="Measure hidden with MigrationNote",
                                        severity='warning',
                                        follow_up=f"Fix table reference '{ref_table}' in measure '{mname}'")
                    break
                elif ref_col not in all_table_fields[ref_table]:
                    measure['isHidden'] = True
                    measure.setdefault('annotations', []).append({
                        'name': 'MigrationNote',
                        'value': f"Self-heal: references non-existent column '{ref_table}'[{ref_col}]. Review and fix."
                    })
                    repairs += 1
                    mname = measure.get('name', '?')
                    print(f"  \u2695 Self-heal: Hidden measure '{mname}' (unknown column '{ref_table}'[{ref_col}])")
                    if recovery:
                        recovery.record('tmdl', 'cross_table_broken_ref',
                                        item_name=mname,
                                        description=f"Measure references non-existent column '{ref_table}'[{ref_col}]",
                                        action="Measure hidden with MigrationNote",
                                        severity='warning',
                                        follow_up=f"Fix column reference [{ref_col}] in measure '{mname}'")
                    break

    # Sprint 136 — Self-Healing v3: 11 additional healers covering common
    # PBI Desktop "won't open" / "data refresh failed" scenarios.
    try:
        from powerbi_import.self_healing_v3 import run_v3_healers
        repairs += run_v3_healers(model, recovery=recovery)
    except Exception:  # never block migration
        pass

    return repairs
