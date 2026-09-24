"""Relationship inference and validation for the semantic model.

Decides which relationships exist between tables, their cardinality and filter
direction, and repairs the ones Power BI cannot load: ambiguous join paths,
many-to-many pairs and cardinality mismatches. Extracted from tmdl_generator so
the relationship surface is one concern in one place; tmdl_generator re-exports
these names for backward compatibility.
"""

import logging
import re

logger = logging.getLogger(__name__)


def _enforce_hybrid_relationship_constraints(model):
    """Enforce oneDirection cross-filtering for relationships spanning storage modes."""
    table_modes = {}
    for table in model['model']['tables']:
        tname = table.get('name', '')
        partitions = table.get('partitions', [])
        mode = partitions[0].get('mode', 'import') if partitions else 'import'
        table_modes[tname] = mode

    for rel in model['model']['relationships']:
        from_mode = table_modes.get(rel.get('fromTable', ''), 'import')
        to_mode = table_modes.get(rel.get('toTable', ''), 'import')
        if from_mode != to_mode:
            rel['crossFilteringBehavior'] = 'oneDirection'


def _create_and_validate_relationships(model, datasources):
    """Phase 4: Create, deduplicate, validate, and fix type mismatches in relationships."""
    seen_rels = set()
    for ds in datasources:
        relationships = ds.get('relationships', [])
        rels = _build_relationships(relationships)
        for rel in rels:
            key = (rel.get('fromTable'), rel.get('fromColumn'),
                   rel.get('toTable'), rel.get('toColumn'))
            if key not in seen_rels:
                seen_rels.add(key)
                model["model"]["relationships"].append(rel)
            else:
                print(f"  ⚠ Skipped duplicate relationship: {key[0]}.{key[1]} → {key[2]}.{key[3]}")

    # Validate relationships: keep only those pointing to existing tables/columns
    valid_relationships = []
    table_columns = {}
    for table in model["model"]["tables"]:
        tname = table.get("name", "")
        table_columns[tname] = {col.get("name", "") for col in table.get("columns", [])}

    def _resolve_rel_column(col_name, table_name, available_cols):
        """Try to resolve a relationship column name to an existing column.

        Handles Tableau renaming patterns:
        - Suffixed: 'Id' → 'Id (TableName)'
        - CamelCase split: 'CreatedById' → 'Created By ID' or 'Created By Id'
        - Case-insensitive match
        """
        if col_name in available_cols:
            return col_name

        # 1. Try with table suffix: col → 'col (table_name)'
        suffixed = f"{col_name} ({table_name})"
        if suffixed in available_cols:
            return suffixed

        # 2. CamelCase → space-separated: 'CreatedById' → 'Created By Id'
        camel_split = re.sub(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])',
                             ' ', col_name)
        if camel_split != col_name:
            if camel_split in available_cols:
                return camel_split
            # Also try with uppercase last word: 'Created By Id' → 'Created By ID'
            parts = camel_split.rsplit(' ', 1)
            if len(parts) == 2 and len(parts[1]) <= 3:
                upper_last = f"{parts[0]} {parts[1].upper()}"
                if upper_last in available_cols:
                    return upper_last

        # 3. Case-insensitive match
        col_lower = col_name.lower()
        for ac in available_cols:
            if ac.lower() == col_lower:
                return ac

        # 4. Case-insensitive CamelCase split
        if camel_split != col_name:
            cs_lower = camel_split.lower()
            for ac in available_cols:
                if ac.lower() == cs_lower:
                    return ac

        return None

    for rel in model["model"]["relationships"]:
        from_table = rel.get("fromTable", "")
        to_table = rel.get("toTable", "")
        from_col = rel.get("fromColumn", "")
        to_col = rel.get("toColumn", "")

        if from_table in table_columns and to_table in table_columns and from_table != to_table:
            resolved_from = _resolve_rel_column(from_col, from_table, table_columns[from_table])
            resolved_to = _resolve_rel_column(to_col, to_table, table_columns[to_table])
            if resolved_from and resolved_to:
                if resolved_from != from_col or resolved_to != to_col:
                    print(f"  ✓ Resolved relationship columns: {from_table}.{from_col}→{resolved_from}, {to_table}.{to_col}→{resolved_to}")
                rel["fromColumn"] = resolved_from
                rel["toColumn"] = resolved_to
                valid_relationships.append(rel)
                continue

        reasons = []
        if from_table not in table_columns:
            reasons.append(f"fromTable '{from_table}' not found")
        elif not _resolve_rel_column(from_col, from_table, table_columns.get(from_table, set())):
            reasons.append(f"fromColumn '{from_col}' not in '{from_table}'")
        if to_table not in table_columns:
            reasons.append(f"toTable '{to_table}' not found")
        elif not _resolve_rel_column(to_col, to_table, table_columns.get(to_table, set())):
            reasons.append(f"toColumn '{to_col}' not in '{to_table}'")
        if from_table == to_table:
            reasons.append("self-join")
        print(f"  ⚠ Dropped relationship: {from_table}.{from_col} → {to_table}.{to_col} ({'; '.join(reasons)})")

    model["model"]["relationships"] = valid_relationships

    # Phase 4b: Fix type mismatches in relationship keys
    _fix_relationship_type_mismatches(model)


def _build_relationships(relationships):
    """
    Create relationships from Tableau joins.

    Args:
        relationships: List of extracted relations with left/right {table, column}

    Returns:
        list: Relationship definitions
    """
    result = []

    for rel in relationships:
        left = rel.get('left', {})
        right = rel.get('right', {})

        from_table = left.get('table', '')
        from_column = left.get('column', '')
        to_table = right.get('table', '')
        to_column = right.get('column', '')

        if not from_table or not to_table or not from_column or not to_column:
            continue

        join_type = rel.get('type', 'left')
        result.append({
            "name": f"Relationship-{len(result)+1}",
            "fromTable": from_table,
            "fromColumn": from_column,
            "toTable": to_table,
            "toColumn": to_column,
            "joinType": join_type,
            "crossFilteringBehavior": "bothDirections" if join_type == 'full' else "oneDirection"
        })

    return result


def _detect_join_graph_issues(relationships):
    """Detect multi-hop chains and diamond joins in the relationship graph.

    Returns a list of warning dicts:
      - {'type': 'diamond', 'tables': [A, B, C, D], 'message': ...}
      - {'type': 'multi_hop', 'chain': [A, B, C], 'message': ...}
    """
    warnings = []
    # Build adjacency list (undirected)
    adj = {}  # table -> set of connected tables
    for rel in relationships:
        ft = rel.get('fromTable', '')
        tt = rel.get('toTable', '')
        if ft and tt:
            adj.setdefault(ft, set()).add(tt)
            adj.setdefault(tt, set()).add(ft)

    # Detect multi-hop chains: A→B→C where A has no direct link to C
    all_tables = list(adj.keys())
    for a in all_tables:
        for b in adj.get(a, set()):
            for c in adj.get(b, set()):
                if c != a and c not in adj.get(a, set()):
                    warnings.append({
                        'type': 'multi_hop',
                        'chain': [a, b, c],
                        'message': (
                            f"Multi-hop join path: '{a}' → '{b}' → '{c}'. "
                            f"PBI may need an intermediate relationship or bridge table."
                        ),
                    })

    # Detect diamond joins: A→B, A→C, B→D, C→D
    for a in all_tables:
        neighbors = list(adj.get(a, set()))
        for i, b in enumerate(neighbors):
            for c in neighbors[i + 1:]:
                shared = adj.get(b, set()) & adj.get(c, set()) - {a}
                for d in shared:
                    warnings.append({
                        'type': 'diamond',
                        'tables': [a, b, c, d],
                        'message': (
                            f"Diamond join: '{a}'→'{b}'→'{d}' and "
                            f"'{a}'→'{c}'→'{d}'. May cause ambiguous paths in PBI."
                        ),
                    })

    # Deduplicate
    seen = set()
    unique = []
    for w in warnings:
        key = (w['type'], tuple(sorted(w.get('chain', w.get('tables', [])))))
        if key not in seen:
            seen.add(key)
            unique.append(w)
    return unique


def _is_parameter_table(tables, table_name):
    """Check if a table is a What-If parameter table (has ParameterTable annotation)."""
    for t in tables:
        if t.get("name", "") == table_name:
            for ann in t.get("annotations", []):
                if ann.get("name") == "ParameterTable":
                    return True
            return False
    return False


def _infer_cross_table_relationships(model):
    """
    Infer relationships between tables when DAX expressions reference
    columns from another table but no explicit relationship exists.

    Algorithm:
    1. Scan all DAX expressions (measures, calc columns, RLS roles)
    2. Find 'TableName'[ColumnName] cross-table references
    3. For each unconnected table pair, find the best column-name match
    4. Create a manyToOne relationship (fact->dimension)
    """
    tables = model["model"]["tables"]
    relationships = model["model"]["relationships"]

    # Build existing relationship pairs (bidirectional)
    connected_pairs = set()
    for rel in relationships:
        ft = rel.get("fromTable", "")
        tt = rel.get("toTable", "")
        connected_pairs.add((ft, tt))
        connected_pairs.add((tt, ft))

    # Build table->columns map
    table_columns = {}
    for table in tables:
        tname = table.get("name", "")
        table_columns[tname] = {col.get("name", "") for col in table.get("columns", [])}

    cross_ref_pattern = re.compile(r"'([^']+)'\[([^\]]+)\]")

    # Collect needed table pairs from DAX cross-table references
    needed_pairs = set()

    for table in tables:
        tname = table.get("name", "")
        for measure in table.get("measures", []):
            expr = measure.get("expression", "")
            for match in cross_ref_pattern.finditer(expr):
                ref_table = match.group(1)
                if ref_table != tname and ref_table in table_columns:
                    needed_pairs.add((tname, ref_table))
        for col in table.get("columns", []):
            if col.get("isCalculated"):
                expr = col.get("expression", "")
                for match in cross_ref_pattern.finditer(expr):
                    ref_table = match.group(1)
                    if ref_table != tname and ref_table in table_columns:
                        needed_pairs.add((tname, ref_table))

    # Scan RLS roles
    for role in model["model"].get("roles", []):
        for tp in role.get("tablePermissions", []):
            perm_table = tp.get("name", "")
            expr = tp.get("filterExpression", "")
            for match in cross_ref_pattern.finditer(expr):
                ref_table = match.group(1)
                if ref_table != perm_table and ref_table in table_columns:
                    needed_pairs.add((perm_table, ref_table))

    # For each needed pair, find a matching column for the relationship
    for (source_table, ref_table) in needed_pairs:
        if (source_table, ref_table) in connected_pairs:
            continue
        # Skip parameter tables — they don't need inferred relationships
        if _is_parameter_table(tables, source_table) or _is_parameter_table(tables, ref_table):
            continue

        source_cols = table_columns.get(source_table, set())
        ref_cols = table_columns.get(ref_table, set())

        best_match = None
        best_score = 0

        for sc in source_cols:
            for rc in ref_cols:
                sc_lower = sc.lower()
                rc_lower = rc.lower()
                score = 0

                if sc_lower == rc_lower:
                    score = 100
                elif sc_lower in rc_lower and len(sc_lower) >= 3:
                    score = 50 - (len(rc) - len(sc))
                elif rc_lower in sc_lower and len(rc_lower) >= 3:
                    score = 50 - (len(sc) - len(rc))
                elif len(sc_lower) >= 3 and len(rc_lower) >= 3:
                    common = 0
                    for a, b in zip(sc_lower, rc_lower):
                        if a == b:
                            common += 1
                        else:
                            break
                    if common >= 3:
                        score = common * 5

                if score > best_score:
                    best_score = score
                    best_match = (sc, rc)

        if best_match and best_score >= 15:
            from_col, to_col = best_match

            if len(source_cols) >= len(ref_cols):
                fact_table, dim_table = source_table, ref_table
                fk_col, pk_col = from_col, to_col
            else:
                fact_table, dim_table = ref_table, source_table
                fk_col, pk_col = to_col, from_col

            relationships.append({
                "name": f"inferred_{fact_table}_{dim_table}",
                "fromTable": fact_table,
                "fromColumn": fk_col,
                "toTable": dim_table,
                "toColumn": pk_col,
                "crossFilteringBehavior": "oneDirection"
            })

            connected_pairs.add((source_table, ref_table))
            connected_pairs.add((ref_table, source_table))

    # Pass 2: Proactive key-column matching for unconnected tables
    # Looks for columns with identical names that look like keys (ID, Key, Code, etc.)
    # Includes English + French business-key vocabulary so shared dimensions
    # like "Numéro d'affaire" are recognized as natural join keys.
    _KEY_SUFFIXES = {
        # English
        'id', 'key', 'code', 'no', 'number', 'num', 'pk', 'fk', 'sk',
        # French (common shared business keys in Tableau data blends)
        'numéro', 'numero', 'identifiant', 'matricule', 'réf', 'ref',
    }
    all_table_names = list(table_columns.keys())

    for i, t1 in enumerate(all_table_names):
        for t2 in all_table_names[i + 1:]:
            if (t1, t2) in connected_pairs:
                continue
            # Skip auto-generated tables (Calendar, parameter tables)
            if t1 == 'Calendar' or t2 == 'Calendar':
                continue
            # Skip parameter tables (What-If) — they should not participate
            # in cross-table relationship inference
            if _is_parameter_table(tables, t1) or _is_parameter_table(tables, t2):
                continue

            t1_cols = table_columns.get(t1, set())
            t2_cols = table_columns.get(t2, set())

            best_col = None
            best_score = 0

            common_cols = t1_cols & t2_cols
            for col in common_cols:
                col_lower = col.lower().rstrip('_')
                # Score: exact ID/key column names get highest priority
                parts = re.split(r'[_\s]', col_lower)
                has_key_suffix = any(p in _KEY_SUFFIXES for p in parts)
                if has_key_suffix:
                    score = 90
                elif col_lower.endswith('name'):
                    score = 40
                else:
                    score = 20  # Any common column
                if score > best_score:
                    best_score = score
                    best_col = col

            if best_col and best_score >= 40:
                # Fact = table with more columns, dim = fewer
                if len(t1_cols) >= len(t2_cols):
                    fact_table, dim_table = t1, t2
                else:
                    fact_table, dim_table = t2, t1

                relationships.append({
                    "name": f"inferred_{fact_table}_{dim_table}",
                    "fromTable": fact_table,
                    "fromColumn": best_col,
                    "toTable": dim_table,
                    "toColumn": best_col,
                    "crossFilteringBehavior": "oneDirection"
                })
                connected_pairs.add((t1, t2))
                connected_pairs.add((t2, t1))


def _detect_many_to_many(model, datasources):
    """
    Determine cardinality for each relationship.

    Strategy — based on Tableau join type + column-count ratio heuristic:
    - Full joins → manyToMany (ambiguous direction)
    - Left/Inner/Right joins:
      - If to-table column count ≥ 70% of from-table → manyToMany (peer/fact tables)
      - If to-table column count < 70% of from-table → manyToOne (lookup table)

    The 70% threshold detects when two tables have similar schemas (both are
    fact tables, e.g. Tableau data blend artifacts) and a manyToOne assumption
    would fail because the 'one' side has duplicates.
    """
    # Build table column count map
    table_col_counts = {}
    for table in model['model'].get('tables', []):
        tname = table.get('name', '')
        table_col_counts[tname] = len(table.get('columns', []))

    # Count Calendar relationships — when >1 table connects to Calendar,
    # use bothDirections so Calendar acts as a shared dimension bridge.
    _cal_rel_count = sum(1 for r in model['model']['relationships']
                         if r.get('toTable') == 'Calendar')

    for rel in model['model']['relationships']:
        to_table = rel.get('toTable', '')
        to_col = rel.get('toColumn', '')
        from_table = rel.get('fromTable', '')
        join_type = rel.get('joinType', 'left')

        if join_type == 'full':
            rel['fromCardinality'] = 'many'
            rel['toCardinality'] = 'many'
            # Single-direction is the safe default for many-to-many: a
            # bidirectional m2m relationship creates ambiguous filter paths
            # that PBI Desktop flags/blocks.
            rel['crossFilteringBehavior'] = 'oneDirection'
            print(f"  ⚠️  Relation → '{to_table}.{to_col}' set to manyToMany (full join, single-direction).")
        else:
            # Column-count ratio heuristic
            from_cols = table_col_counts.get(from_table, 0)
            to_cols = table_col_counts.get(to_table, 0)

            # Check if this is an inferred relationship (Phase 10) joining on
            # a non-key column — default to manyToMany since we can't verify
            # uniqueness without data.
            rel_name = rel.get('name', '')
            is_inferred = rel_name.startswith('inferred_')
            to_col_lower = to_col.lower()
            _key_indicators = {'id', 'key', 'code', 'pk', 'fk', 'sk', 'no', 'number', 'num'}
            _key_sep_re = re.compile(r'(?:^|[_\s])(' + '|'.join(_key_indicators) + r')(?:$|[_\s])', re.IGNORECASE)
            is_key_column = bool(_key_sep_re.search(to_col_lower)) or to_col_lower in _key_indicators

            if is_inferred and not is_key_column:
                # Inferred relationship on a non-key column → manyToMany (safe default)
                rel['fromCardinality'] = 'many'
                rel['toCardinality'] = 'many'
                rel['crossFilteringBehavior'] = 'oneDirection'
                print(f"  ⚠️  Relation → '{to_table}.{to_col}' set to manyToMany (inferred, non-key column, single-direction).")
            elif from_cols > 0 and to_cols >= 0.7 * from_cols:
                # Both tables have similar column counts → peer/fact tables
                rel['fromCardinality'] = 'many'
                rel['toCardinality'] = 'many'
                rel['crossFilteringBehavior'] = 'oneDirection'
                print(f"  ⚠️  Relation → '{to_table}.{to_col}' set to manyToMany (peer table, {to_cols}/{from_cols} cols ≥ 70%, single-direction).")
            elif to_table == 'Calendar':
                # Calendar.Date is guaranteed unique (generated table)
                rel['fromCardinality'] = 'many'
                rel['toCardinality'] = 'one'
                # Use bothDirections when multiple tables connect to Calendar
                # so Calendar acts as a shared dimension bridge (star schema).
                if _cal_rel_count > 1:
                    rel['crossFilteringBehavior'] = 'bothDirections'
                    print(f"  ✓  Relation → '{to_table}.{to_col}' set to manyToOne bothDirections (Calendar bridge).")
                else:
                    rel['crossFilteringBehavior'] = 'oneDirection'
                    print(f"  ✓  Relation → '{to_table}.{to_col}' set to manyToOne (Calendar table).")
            else:
                # Default to manyToMany — we cannot verify uniqueness without data
                # PBI silently drops manyToOne relationships if the "one" side has duplicates
                rel['fromCardinality'] = 'many'
                rel['toCardinality'] = 'many'
                rel['crossFilteringBehavior'] = 'oneDirection'
                print(f"  ⚠️  Relation → '{to_table}.{to_col}' set to manyToMany (cannot verify uniqueness, single-direction).")


def _fix_relationship_type_mismatches(model):
    """
    Fix type mismatches between relationship key columns.
    Aligns the toColumn ('one' side) to the fromColumn ('many' side) type.
    """
    tables = {t.get('name', ''): t for t in model['model']['tables']}

    pbi_to_m = {
        'String': 'type text',
        'string': 'type text',
        'Int64': 'Int64.Type',
        'int64': 'Int64.Type',
        'Double': 'type number',
        'double': 'type number',
        'Boolean': 'type logical',
        'boolean': 'type logical',
        'DateTime': 'type datetime',
        'dateTime': 'type datetime',
    }

    for rel in model['model']['relationships']:
        from_table = tables.get(rel.get('fromTable', ''))
        to_table = tables.get(rel.get('toTable', ''))
        if not from_table or not to_table:
            continue

        from_col_name = rel.get('fromColumn', '')
        to_col_name = rel.get('toColumn', '')

        from_col = next((c for c in from_table.get('columns', []) if c.get('name') == from_col_name), None)
        to_col = next((c for c in to_table.get('columns', []) if c.get('name') == to_col_name), None)
        if not from_col or not to_col:
            continue

        from_type = from_col.get('dataType', 'string')
        to_type = to_col.get('dataType', 'string')

        if from_type == to_type:
            continue

        print(f"  \u26a0\ufe0f  Type mismatch: {rel.get('fromTable')}.{from_col_name} ({from_type}) "
              f"-> {rel.get('toTable')}.{to_col_name} ({to_type}). Aligning to {from_type}.")

        old_type = to_type
        to_col['dataType'] = from_type

        if from_type.lower() == 'string':
            to_col['summarizeBy'] = 'none'
            if 'formatString' in to_col:
                del to_col['formatString']

        old_m_type = pbi_to_m.get(old_type, '')
        new_m_type = pbi_to_m.get(from_type, '')
        if old_m_type and new_m_type:
            for partition in to_table.get('partitions', []):
                source = partition.get('source', {})
                if isinstance(source, dict) and 'expression' in source:
                    expr = source['expression']
                    old_pattern = f'"{to_col_name}", {old_m_type}'
                    new_pattern = f'"{to_col_name}", {new_m_type}'
                    if old_pattern in expr:
                        source['expression'] = expr.replace(old_pattern, new_pattern)
        else:
            print(f"    \u26a0\ufe0f  Cannot map M types for {to_col_name}: {repr(old_type)} / {repr(from_type)}")


def _deactivate_ambiguous_paths(model):
    """
    Detect and deactivate relationships that create ambiguous paths.

    Power BI requires that the graph of active relationships forms a forest
    (tree per connected component) — i.e., no cycles when treated as undirected.
    If a cycle is detected, the least-important relationship is deactivated.

    Priority for deactivation (first deactivated):
      1. Auto-generated Calendar relationships (name starts with 'Calendar_')
      2. Inferred cross-table relationships (name starts with 'inferred_')
      3. Original Tableau-extracted relationships (last resort)
    """
    relationships = model["model"]["relationships"]
    if not relationships:
        return

    # --- Union-Find -------------------------------------------------------
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])  # path compression
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return False          # cycle detected
        parent[ra] = rb
        return True

    # --- Sort relationships so the most important are added first ----------
    def _deactivation_priority(rel):
        """Lower value = more important = added to tree first."""
        name = rel.get('name', '')
        if name.startswith('Calendar_'):
            return 2   # auto-generated → deactivate first
        if name.startswith('inferred_'):
            return 1   # inferred → deactivate second
        return 0       # original Tableau relationships → keep

    sorted_rels = sorted(relationships, key=_deactivation_priority)

    deactivated = []
    for rel in sorted_rels:
        if rel.get('isActive') == False:
            continue  # already inactive, skip
        from_t = rel.get('fromTable', '')
        to_t = rel.get('toTable', '')
        if not from_t or not to_t:
            continue
        if not union(from_t, to_t):
            # This edge creates a cycle → deactivate it
            rel['isActive'] = False
            deactivated.append(f"{from_t}.{rel.get('fromColumn','')} → "
                               f"{to_t}.{rel.get('toColumn','')}")

    for d in deactivated:
        print(f"  ⚠ Deactivated relationship (ambiguous path): {d}")
