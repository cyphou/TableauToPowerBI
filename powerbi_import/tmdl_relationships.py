"""Relationship inference and validation for the semantic model.

Decides which relationships exist between tables, their cardinality and filter
direction, and repairs the ones Power BI cannot load: ambiguous join paths,
many-to-many pairs and cardinality mismatches. Extracted from tmdl_generator so
the relationship surface is one concern in one place; tmdl_generator re-exports
these names for backward compatibility.
"""

import hashlib
import json
import logging
import os
import re
import uuid

from tableau_export.m_query_builder import _m_header_literal
from powerbi_import.errors import PartialGenerationError

logger = logging.getLogger(__name__)


def _predicate_equality_pairs(predicate):
    """Return only fully resolved conjunctive field equalities."""
    if not isinstance(predicate, dict):
        return []
    operator = str(predicate.get('operator', '')).upper()
    if operator == 'AND':
        pairs = []
        for argument in predicate.get('arguments', []):
            child = _predicate_equality_pairs(argument)
            if not child:
                return []
            pairs.extend(child)
        return pairs
    if operator not in ('=', '=='):
        return []
    left, right = predicate.get('left'), predicate.get('right')
    if not all(isinstance(side, dict) for side in (left, right)):
        return []
    if not all(isinstance(side.get(key), str) and side[key]
               for side in (left, right) for key in ('table', 'column')):
        return []
    return [((left['table'], left['column']), (right['table'], right['column']))]


def _resolved_single_equality(predicate):
    """Return one declared field equality in source order, or ``None``."""
    pairs = _predicate_equality_pairs(predicate)
    return pairs[0] if len(pairs) == 1 else None


def _physical_join_nodes(datasource):
    """Yield source-path/node pairs for every active physical join tree."""
    metadata = datasource.get('relation_metadata') or {}

    def walk(node, path):
        if not isinstance(node, dict):
            return
        if str(node.get('type', '')).casefold() == 'join':
            yield path, node
        for index, child in enumerate(node.get('children') or []):
            yield from walk(child, f'{path}.children[{index}]')

    for index, node in enumerate(metadata.get('physical_relations') or []):
        yield from walk(node, f'physical_relations[{index}]')
    for table_index, logical_table in enumerate(metadata.get('logical_tables') or []):
        if not isinstance(logical_table, dict):
            continue
        for relation_index, node in enumerate(logical_table.get('relations') or []):
            yield from walk(
                node, f'logical_tables[{table_index}].relations[{relation_index}]')


def _physical_aliases(node):
    if not isinstance(node, dict):
        return set()
    kind = str(node.get('type', '')).casefold()
    if kind in ('table', 'text'):
        return {node['name']} if isinstance(node.get('name'), str) and node['name'] else set()
    return set().union(*(
        _physical_aliases(child) for child in node.get('children') or []
    ))


def _physical_join_sides(node):
    """Return the physical table aliases under the declared left and right sides."""
    children = node.get('children') or [] if isinstance(node, dict) else []
    if len(children) != 2:
        return set(), set()
    return _physical_aliases(children[0]), _physical_aliases(children[1])


def _single_physical_join_equality(node):
    """Return a single equality whose endpoints belong to opposite join sides."""
    if not isinstance(node, dict) or str(node.get('type', '')).casefold() != 'join':
        return None
    pairs = _predicate_equality_pairs(node.get('predicate'))
    if len(pairs) != 1:
        return None
    left, right = _physical_join_sides(node)
    first, second = pairs[0]
    if ((first[0] in left and second[0] in right)
            or (first[0] in right and second[0] in left)):
        return pairs[0]
    return None


def _physical_join_scopes(datasource):
    """Map every source alias pair covered by a join to its exact tree paths."""
    scopes = {}
    for path, node in _physical_join_nodes(datasource):
        left, right = _physical_join_sides(node)
        equalities = _predicate_equality_pairs(node.get('predicate'))
        for left_alias in left:
            for right_alias in right:
                pair = frozenset((left_alias, right_alias))
                scope = scopes.setdefault(pair, {'paths': [], 'equalities': []})
                if path not in scope['paths']:
                    scope['paths'].append(path)
                for equality in equalities:
                    if frozenset((equality[0][0], equality[1][0])) == pair \
                            and frozenset(equality) not in scope['equalities']:
                        scope['equalities'].append(frozenset(equality))
    return scopes


def preserve_physical_join_provenance(datasources, merged_datasource):
    """Retain validated native trees on a shared datasource without inventing edges."""
    result = dict(merged_datasource)
    metadata = dict(result.get('relation_metadata') or {})
    physical_relations = list(metadata.get('physical_relations') or [])
    for datasource in datasources:
        source_metadata = datasource.get('relation_metadata') or {}
        physical_relations.extend(source_metadata.get('physical_relations') or [])
    metadata.update({
        'version': 1,
        'physical_relations': physical_relations,
        'logical_tables': [],
        'logical_relationships': [],
    })
    result['relation_metadata'] = metadata
    result['_retained_physical_join_provenance'] = True
    return result


def preflight_physical_joins(datasources, *, provenance_retained=True,
                             allow_nested_retention=False,
                             tolerate_unrepresentable=False):
    """Reject active physical joins whose complete row predicate is not retained.

    Exact materialized extracts replace the active source tree during Tableau
    extraction. A bare materialization claim or historical native metadata does
    not. Shared-model preflight may temporarily allow a nested tree only so its
    dedicated remapper can prove and retain the complete scope. PBIP callers
    may opt to receive omission diagnostics instead of an exception; this does
    not make any partial equality safe to emit.
    """
    diagnostics = []
    omitted_pairs = {}

    def omit(source, item, aliases, message, required_evidence):
        source_key = str(source or '')
        display_pairs = [
            sorted((left_alias, right_alias), key=str.casefold)
            for left_alias in aliases.get('left', ())
            for right_alias in aliases.get('right', ())
            if left_alias.casefold() != right_alias.casefold()
        ]
        pair_keys = {tuple(value.casefold() for value in pair)
                     for pair in display_pairs}
        diagnostic = {
            'code': 'physical_join_omitted',
            'severity': 'warning',
            'source': source_key,
            'item': item,
            'aliases': sorted(
                set(aliases.get('left', ())) | set(aliases.get('right', ())),
                key=str.casefold,
            ),
            'omitted_relationship_pairs': [list(pair) for pair in sorted(set(
                tuple(pair) for pair in display_pairs))],
            'omitted_relationships': [],
            'message': message,
            'impact': ('No Power BI model relationship was emitted for the affected '
                       'table pair. Dependent measures or visuals may be blank or '
                       'incorrect until the join is materialized or repaired.'),
            'required_evidence': list(required_evidence),
        }
        diagnostics.append(diagnostic)
        for pair in pair_keys:
            omitted_pairs.setdefault((source_key, pair), diagnostic)

    def reject_or_record(error, source, item, left, right):
        if not tolerate_unrepresentable:
            raise error
        omit(source, item, {'left': left, 'right': right}, str(error),
             error.required_evidence)

    for datasource in datasources or []:
        if not isinstance(datasource, dict):
            continue
        retained = datasource.get('_retained_physical_join_provenance') is True
        scopes = _physical_join_scopes(datasource)
        for path, node in _physical_join_nodes(datasource):
            if _single_physical_join_equality(node) is not None \
                    and node.get('requires_materialization') is not True:
                continue
            if retained and provenance_retained:
                continue
            if allow_nested_retention and '.children[' in path:
                continue
            left, right = _physical_join_sides(node)
            aliases = sorted(left | right, key=str.casefold)
            message = (
                'The active physical join predicate cannot be represented as a '
                'Power BI relationship without materializing the complete source '
                f'join; involved aliases: {", ".join(aliases) or "unknown"}.'
            )
            if not provenance_retained:
                message += ' The merge would discard the required physical join scope.'
            error = PartialGenerationError(
                message,
                item=f'relation_metadata.{path}',
                source=datasource.get('name') or datasource.get('caption'),
                recovery_kind='physical_join_predicate',
                required_evidence=[
                    'Complete, unsampled materialization of the full physical join',
                    'Verified source-row and column lineage for every join predicate',
                    'Preserved join scope and declared equality relationships',
                ],
            )
            reject_or_record(
                error, datasource.get('name') or datasource.get('caption'),
                f'relation_metadata.{path}', left, right)

        if scopes:
            allowed = {
                frozenset((edge[0], edge[1]))
                for _, node in _physical_join_nodes(datasource)
                for edge in (_predicate_equality_pairs(node.get('predicate')))
            }
            for relation in datasource.get('relationships') or []:
                if not isinstance(relation, dict):
                    continue
                left_side, right_side = relation.get('left') or {}, relation.get('right') or {}
                if not isinstance(left_side, dict) or not isinstance(right_side, dict):
                    continue
                edge = ((left_side.get('table'), left_side.get('column')),
                        (right_side.get('table'), right_side.get('column')))
                pair = frozenset((edge[0][0], edge[1][0]))
                source_key = str(datasource.get('name') or datasource.get('caption') or '')
                pair_key = tuple(sorted(
                    (str(edge[0][0]).casefold(), str(edge[1][0]).casefold())))
                if (source_key, pair_key) in omitted_pairs:
                    diagnostic = omitted_pairs[source_key, pair_key]
                    diagnostic['omitted_relationships'].append({
                        'from_table': edge[0][0], 'from_column': edge[0][1],
                        'to_table': edge[1][0], 'to_column': edge[1][1],
                    })
                    continue
                if (pair in scopes and relation.get('inferred') is True
                        and frozenset(edge) not in allowed):
                    error = PartialGenerationError(
                        'An inferred relationship conflicts with the declared '
                        'physical join predicate.',
                        item='relationships',
                        source=datasource.get('name') or datasource.get('caption'),
                        recovery_kind='physical_join_predicate',
                        required_evidence=[
                            'Relationship endpoints matching the complete declared physical equality',
                        ],
                    )
                    reject_or_record(
                        error, source_key, 'relationships',
                        {edge[0][0]}, {edge[1][0]})
                    diagnostic = omitted_pairs.get((source_key, pair_key))
                    if diagnostic is not None:
                        diagnostic['omitted_relationships'].append({
                            'from_table': edge[0][0], 'from_column': edge[0][1],
                            'to_table': edge[1][0], 'to_column': edge[1][1],
                        })
    return diagnostics


def _composite_equality_terms(predicate, left_alias, right_alias):
    """Return ordered alias-to-column equality pairs, or None if not proven."""
    terms = predicate.get('arguments', []) if str(
        predicate.get('operator', '')).upper() == 'AND' else [predicate]
    if len(terms) < 2:
        return None
    result = []
    seen = set()
    for term in terms:
        if str(term.get('operator', '')).upper() not in ('=', '=='):
            return None
        left, right = term.get('left'), term.get('right')
        if not isinstance(left, dict) or not isinstance(right, dict):
            return None
        if (left.get('table'), right.get('table')) == (left_alias, right_alias):
            pair = (left.get('column'), right.get('column'))
        elif (left.get('table'), right.get('table')) == (right_alias, left_alias):
            pair = (right.get('column'), left.get('column'))
        else:
            return None
        if not all(isinstance(value, str) and value for value in pair) or pair in seen:
            return None
        seen.add(pair)
        result.append(pair)
    return result


def _composite_key_m_expression(terms, *, side, nulls_match, row_name):
    """Build a type-tagged, length-prefixed key with collision-free text encoding."""
    encoded = []
    for index, (column, data_type) in enumerate(terms):
        if '\0' in column:
            raise ValueError("Composite relationship column contains a null character")
        reference = '[#' + _m_header_literal(column) + ']'
        normalized = str(data_type or '').casefold().replace(' ', '')
        if normalized in ('string', 'text', 'varchar', 'nvarchar'):
            value = (
                f'Binary.ToText(Text.ToBinary({reference}, TextEncoding.Utf8), '
                'BinaryEncoding.Hex)'
            )
            type_tag = 'S'
        elif normalized in ('integer', 'int64', 'wholenumber', 'whole'):
            value = f'Number.ToText({reference}, "0", "en-US")'
            type_tag = 'I'
        elif normalized in ('boolean', 'bool', 'logical'):
            value = f'if {reference} then "1" else "0"'
            type_tag = 'B'
        elif normalized in ('date', 'date32'):
            value = (
                f'Date.ToText({reference}, '
                '[Format = "yyyy-MM-dd", Culture = "en-US"])'
            )
            type_tag = 'D'
        elif normalized in ('datetime', 'datetime2', 'timestamp'):
            value = (
                f'DateTime.ToText({reference}, '
                '[Format = "yyyy-MM-ddTHH:mm:ss.fffffff", Culture = "en-US"])'
            )
            type_tag = 'T'
        else:
            raise ValueError(
                f"Composite equality key type {data_type!r} is unsupported; "
                "use matching text, integer, boolean, date, or datetime columns.")
        encoded.append((value, type_tag, reference))

    segments = []
    for value, type_tag, _ in encoded:
        segments.append(
            f'"{type_tag}" & Text.From(Text.Length({value}), "en-US") '
            f'& ":" & {value}'
        )
    serialized = ' & '.join(segments)
    null_test = ' or '.join(f'{reference} = null' for _, _, reference in encoded)
    if nulls_match:
        serialized = ' & '.join(
            f'(if {reference} = null then "N0:" else {segment})'
            for (_, _, reference), segment in zip(encoded, segments)
        )
        result = serialized
    else:
        null_value = f'"N:{side}:" & Text.From([#"{row_name}"], "en-US")'
        result = f'if {null_test} then {null_value} else {serialized}'
    return result


def _composite_key_type(value):
    normalized = str(value or '').casefold().replace(' ', '')
    if normalized in ('string', 'text', 'varchar', 'nvarchar'):
        return 'string'
    if normalized in ('integer', 'int64', 'whole', 'wholenumber'):
        return 'integer'
    if normalized in ('boolean', 'bool', 'logical'):
        return 'boolean'
    if normalized in ('date', 'date32'):
        return 'date'
    if normalized in ('datetime', 'datetime2', 'timestamp'):
        return 'datetime'
    return None


def _materialize_composite_logical_relationships(model, extra_objects):
    """Materialize explicitly governed composite equalities as Import relationships.

    The generated key is an unambiguous encoding, not a delimited concatenation.
    The declared one-side uniqueness is checked on every Power Query refresh.
    """
    policies = extra_objects.get('composite_relationship_policies')
    policies_from_environment = policies is None
    if policies_from_environment:
        raw = os.environ.get('TTPBI_COMPOSITE_RELATIONSHIP_POLICIES', '{}')
        try:
            policies = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid TTPBI_COMPOSITE_RELATIONSHIP_POLICIES JSON") from exc
    if not isinstance(policies, dict):
        raise TypeError("composite_relationship_policies must be datasource-keyed")
    if any(not isinstance(name, str) for name in policies):
        raise TypeError("composite_relationship_policies keys must be datasource names")

    tables = {table.get('name'): table
              for table in model.get('model', {}).get('tables', [])}
    datasources = model.get('_datasources', [])
    table_renames = model.get('_table_rename_map', {})
    column_renames = model.get('_column_rename_map', {})
    plans = []
    known_datasources = set()

    for datasource in datasources:
        ds_name = datasource.get('name', '')
        known_datasources.add(ds_name)
        ds_policies = policies.get(ds_name, {})
        if not isinstance(ds_policies, dict):
            raise TypeError(
                f"Composite relationship policies for {ds_name!r} must be an object")
        if any(not isinstance(index, str) for index in ds_policies):
            raise TypeError(
                f"Composite relationship policy indices for {ds_name!r} must be strings")
        matched_policy_indices = set()
        for index, relation in enumerate(
                datasource.get('relation_metadata', {}).get('logical_relationships', [])):
            endpoints = [relation.get(side, {}).get('tables', [])
                         for side in ('left', 'right')]
            if any(len(side) != 1 for side in endpoints):
                continue
            left_alias, right_alias = endpoints[0][0], endpoints[1][0]
            pairs = _composite_equality_terms(
                relation.get('predicate', {}), left_alias, right_alias)
            if pairs is None:
                continue
            raw_policy = ds_policies.get(str(index))
            if raw_policy is None:
                continue
            matched_policy_indices.add(str(index))
            if not isinstance(raw_policy, dict):
                raise TypeError(
                    f"Composite relationship policy {ds_name}[{index}] must be an object")
            many_to_many = raw_policy.get('cardinality') == 'manyToMany'
            if many_to_many:
                required = {
                    'cardinality', 'comparison_case_sensitive', 'comparison_exact',
                    'nulls_match', 'filter_direction',
                }
                filter_direction = raw_policy.get('filter_direction')
                if filter_direction == 'oneDirection':
                    required.add('filter_from')
                elif filter_direction != 'bothDirections':
                    raise ValueError(
                        f"Composite relationship {ds_name}[{index}] requires an "
                        "explicit oneDirection or bothDirections filter policy")
                if set(raw_policy) != required:
                    raise ValueError(
                        f"Composite relationship policy {ds_name}[{index}] must "
                        f"contain exactly {sorted(required)!r}")
                if (filter_direction == 'oneDirection'
                        and raw_policy.get('filter_from') not in ('left', 'right')):
                    raise ValueError(
                        f"Composite relationship policy {ds_name}[{index}] "
                        "filter_from must be 'left' or 'right'")
            else:
                required = {
                    'one_side', 'comparison_case_sensitive', 'comparison_exact',
                    'nulls_match', 'filter_direction',
                }
                if set(raw_policy) != required:
                    raise ValueError(
                        f"Composite relationship policy {ds_name}[{index}] must contain "
                        "only one_side, comparison_case_sensitive, comparison_exact, "
                        "nulls_match, and filter_direction")
                if raw_policy.get('one_side') not in ('left', 'right'):
                    raise ValueError(
                        f"Composite relationship policy {ds_name}[{index}] one_side "
                        "must be 'left' or 'right'")
            if raw_policy.get('comparison_case_sensitive') is not True:
                raise ValueError(
                    f"Composite relationship {ds_name}[{index}] requires verified "
                    "case-sensitive source equality")
            if raw_policy.get('comparison_exact') is not True:
                raise ValueError(
                    f"Composite relationship {ds_name}[{index}] requires verified "
                    "exact source string equality, including accent and trailing-space "
                    "semantics")
            if not isinstance(raw_policy.get('nulls_match'), bool):
                raise TypeError(
                    f"Composite relationship policy {ds_name}[{index}] must explicitly "
                    "declare whether null tuples match")
            if not many_to_many and raw_policy.get('filter_direction') != 'oneDirection':
                raise ValueError(
                    f"Composite relationship {ds_name}[{index}] supports only an "
                    "explicit oneDirection filter policy")
            left_table = table_renames.get((ds_name, left_alias), left_alias)
            right_table = table_renames.get((ds_name, right_alias), right_alias)
            if left_table == right_table or left_table not in tables or right_table not in tables:
                raise ValueError(
                    f"Composite relationship {ds_name}[{index}] does not resolve "
                    "to two distinct generated model tables")

            source_tables = {}
            for source_table in datasource.get('tables', []):
                source_tables.setdefault(source_table.get('name'), []).append(
                    source_table)

            def resolve_terms(
                    alias, model_table, field_names, *, source_tables=source_tables,
                    ds_name=ds_name, index=index, column_renames=column_renames,
                    tables=tables):
                matching_tables = source_tables.get(alias, [])
                if len(matching_tables) != 1:
                    raise ValueError(
                        f"Composite relationship {ds_name}[{index}] source alias "
                        f"{alias!r} has missing or ambiguous table metadata")
                source_columns = {}
                for source_column in matching_tables[0].get('columns', []):
                    source_columns.setdefault(
                        str(source_column.get('name', '')).strip('[]'), []).append(
                            source_column)
                result = []
                model_columns = tables[model_table].get('columns', [])
                for field_name in field_names:
                    matching_columns = source_columns.get(field_name, [])
                    if len(matching_columns) != 1:
                        raise ValueError(
                            f"Composite relationship {ds_name}[{index}] cannot resolve "
                            f"or has ambiguous source field {alias}.{field_name}")
                    source_column = matching_columns[0]
                    produced_name = column_renames.get(
                        (model_table, field_name), field_name)
                    matches = [column for column in model_columns
                               if column.get('sourceColumn') == produced_name
                               or column.get('name') == produced_name]
                    if len(matches) != 1:
                        raise ValueError(
                            f"Composite relationship {ds_name}[{index}] has ambiguous "
                            f"generated field {model_table}.{produced_name}")
                    data_type = source_column.get('datatype')
                    if not data_type:
                        raise ValueError(
                            f"Composite relationship {ds_name}[{index}] is missing "
                            f"type metadata for {alias}.{field_name}")
                    model_column = matches[0]
                    source_name = model_column.get('sourceColumn')
                    if (not isinstance(source_name, str) or not source_name
                            or model_column.get('expression')
                            or model_column.get('isCalculated')):
                        raise ValueError(
                            f"Composite relationship {ds_name}[{index}] requires "
                            f"source-bound M columns; {model_table}.{produced_name} "
                            "is not available in the Import partition")
                    model_type = model_column.get('dataType')
                    if (model_type
                            and _composite_key_type(model_type)
                            != _composite_key_type(data_type)):
                        raise ValueError(
                            f"Composite relationship {ds_name}[{index}] has "
                            f"mismatched model and source types for "
                            f"{model_table}.{produced_name}")
                    result.append((source_name, data_type))
                return result

            left_terms = resolve_terms(left_alias, left_table,
                                       [pair[0] for pair in pairs])
            right_terms = resolve_terms(right_alias, right_table,
                                        [pair[1] for pair in pairs])
            if ([_composite_key_type(t[1]) for t in left_terms] !=
                    [_composite_key_type(t[1]) for t in right_terms]
                    or any(_composite_key_type(t[1]) is None
                           for t in left_terms + right_terms)):
                raise ValueError(
                    f"Composite relationship {ds_name}[{index}] has mismatched "
                    "key component types")
            one_table = None if many_to_many else (
                left_table if raw_policy['one_side'] == 'left' else right_table)
            many_table = None if many_to_many else (
                right_table if raw_policy['one_side'] == 'left' else left_table)
            plans.append({
                'datasource': ds_name, 'index': index,
                'left_table': left_table, 'right_table': right_table,
                'left_terms': left_terms, 'right_terms': right_terms,
                'one_table': one_table, 'many_table': many_table,
                'one_terms': None if many_to_many else (
                    left_terms if raw_policy['one_side'] == 'left' else right_terms),
                'many_terms': None if many_to_many else (
                    right_terms if raw_policy['one_side'] == 'left' else left_terms),
                'key_components': [
                    {'left_column': left_name, 'right_column': right_name}
                    for left_name, right_name in pairs
                ],
                'cardinality': 'manyToMany' if many_to_many else 'manyToOne',
                'policy': raw_policy,
            })
        unknown_indices = set(ds_policies) - matched_policy_indices
        if unknown_indices:
            raise ValueError(
                f"Composite relationship policy indices for {ds_name!r} do not "
                f"identify supported composite equalities: {sorted(unknown_indices)!r}")

    unknown_datasources = set(policies) - known_datasources
    if unknown_datasources:
        if not policies_from_environment:
            raise ValueError(
                "Composite relationship policies reference unknown datasources: "
                f"{sorted(unknown_datasources)!r}")
        logger.info(
            "Composite relationship policies for other datasources were not "
            "applied to this model: %s", sorted(unknown_datasources))

    if not plans:
        return
    from powerbi_import.tmdl_m_conversion import inject_m_steps

    existing_relations = set()
    for plan in plans:
        identity = frozenset((plan['left_table'], plan['right_table']))
        if identity in existing_relations:
            raise ValueError(
                "Multiple composite logical relationships between the same model "
                "tables require an explicit shared bridge and are not supported")
        existing_relations.add(identity)

    prepared = []
    staged_partitions = {}
    planned_columns = {}
    for plan in plans:
        tag_seed = f"{plan['datasource']}\0{plan['index']}".encode()
        tag = hashlib.sha256(tag_seed).hexdigest()[:32].upper()
        key_name = f'__TTPBI_CompositeKey_{tag}'
        row_name = f'__TTPBI_CompositeRow_{tag}'
        key_names = {key_name.casefold(), row_name.casefold()}
        partitions = {}
        for table_name in (plan['left_table'], plan['right_table']):
            table = tables[table_name]
            existing_names = {
                name.casefold()
                for column in table.get('columns', [])
                for name in (column.get('name'), column.get('sourceColumn'))
                if isinstance(name, str)
            }
            if key_names & (existing_names | planned_columns.get(table_name, set())):
                raise ValueError(
                    f"Composite relationship helper column collides in {table_name}")
            table_partitions = table.get('partitions')
            if (not isinstance(table_partitions, list) or len(table_partitions) != 1
                    or not isinstance(table_partitions[0], dict)
                    or table_partitions[0].get('mode') != 'import'
                    or not isinstance(table_partitions[0].get('source'), dict)
                    or table_partitions[0]['source'].get('type') != 'm'
                    or not table_partitions[0]['source'].get('expression')):
                raise ValueError(
                    f"Composite relationship {plan['datasource']}[{plan['index']}] "
                    f"requires a single Import M partition for {table_name}")
            partitions[table_name] = table_partitions[0]
            planned_columns.setdefault(table_name, set()).update(key_names)

        expressions = {}
        many_to_many = plan['cardinality'] == 'manyToMany'
        if many_to_many:
            keyed_tables = (
                (tables[plan['left_table']], plan['left_terms'], 'L'),
                (tables[plan['right_table']], plan['right_terms'], 'R'),
            )
        else:
            one = tables[plan['one_table']]
            many = tables[plan['many_table']]
            keyed_tables = (
                (one, plan['one_terms'],
                 'L' if plan['one_table'] == plan['left_table'] else 'R'),
                (many, plan['many_terms'],
                 'L' if plan['many_table'] == plan['left_table'] else 'R'),
            )
        for table, terms, side in keyed_tables:
            expressions[table['name']] = _composite_key_m_expression(
                terms, side=side,
                nulls_match=plan['policy']['nulls_match'],
                row_name=row_name,
            )
            table_literal = _m_header_literal(table['name'])
            error_message = _m_header_literal(
                f"Declared one-side key is not unique in {table['name']}")
            steps = [
                (f'#"TTPBI Composite Row {tag}"', (
                    f'Table.AddIndexColumn({{prev}}, "{row_name}", 0, 1, Int64.Type)')),
                (f'#"TTPBI Composite Key {tag}"', (
                    f'Table.AddColumn({{prev}}, "{key_name}", each '
                    f'({expressions[table["name"]]}), type text)')),
                (f'#"TTPBI Remove Composite Row {tag}"', (
                    f'Table.RemoveColumns({{prev}}, {{"{row_name}"}})')),
            ]
            if not many_to_many and table is one:
                steps.append((f'#"TTPBI Composite Key Check {tag}"', (
                    f'let __rows = {{prev}}, '
                    f'__distinct = Table.RowCount(Table.Distinct(__rows, {{"{key_name}"}})) '
                    f'in if __distinct <> Table.RowCount(__rows) then '
                    'error Error.Record("Migration.CompositeKeyNotUnique", '
                    f'{error_message}, '
                    f'[Table = {table_literal}, Key = "{key_name}"]) else __rows'
                )))
            partition = partitions[table['name']]
            source = partition['source']
            previous = staged_partitions.get(table['name'])
            current_expression = previous[1] if previous else source['expression']
            updated = inject_m_steps(current_expression, steps)
            if f'#"TTPBI Composite Key {tag}"' not in updated:
                raise ValueError(
                    f"Could not inject composite key into Import M partition "
                    f"for {table['name']}")
            staged_partitions[table['name']] = (partition, updated)
        prepared.append({
            **plan, 'tag': tag, 'key_name': key_name, 'row_name': row_name,
            'expressions': expressions,
        })

    for partition, expression in staged_partitions.values():
        partition['source']['expression'] = expression

    for plan in prepared:
        key_name = plan['key_name']
        many_to_many = plan['cardinality'] == 'manyToMany'
        if many_to_many:
            left = tables[plan['left_table']]
            right = tables[plan['right_table']]
            relationship_from = left
            relationship_to = right
            filter_direction = plan['policy']['filter_direction']
            if filter_direction == 'oneDirection':
                filter_source = left if plan['policy']['filter_from'] == 'left' else right
                relationship_from = right if filter_source is left else left
                relationship_to = filter_source
            relationship = {
                'name': str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"ttpbi:composite:{plan['datasource']}:{plan['index']}:"
                    f"{relationship_from['name']}:{relationship_to['name']}",
                )),
                'fromTable': relationship_from['name'],
                'fromColumn': key_name,
                'toTable': relationship_to['name'],
                'toColumn': key_name,
                'fromCardinality': 'many', 'toCardinality': 'many',
                'joinType': 'left', 'crossFilteringBehavior': filter_direction,
                '_explicit_composite_cardinality': True,
            }
        else:
            one = tables[plan['one_table']]
            many = tables[plan['many_table']]
            relationship = {
                'name': str(uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"ttpbi:composite:{plan['datasource']}:{plan['index']}:"
                    f"{plan['many_table']}:{plan['one_table']}",
                )),
                'fromTable': plan['many_table'], 'fromColumn': key_name,
                'toTable': plan['one_table'], 'toColumn': key_name,
                'joinType': 'left', 'crossFilteringBehavior': 'oneDirection',
                '_source_cardinality_verified': True,
            }
        for table in ((left, right) if many_to_many else (one, many)):
            table['columns'].append({
                'name': key_name, 'dataType': 'String',
                'sourceColumn': key_name, 'summarizeBy': 'none',
                'isHidden': True, 'displayFolder': 'Technical',
                'description': 'Refresh-validated composite logical relationship key.',
            })

        model.setdefault('_logical_predicate_pairs', []).append(
            [plan['left_table'], plan['right_table']])
        model.setdefault('_logical_equality_compiled_pairs', []).append(
            [plan['left_table'], plan['right_table']])
        model.setdefault('_materialized_composite_relationships', []).append(relationship)
        if many_to_many:
            model.setdefault('_composite_relationship_migrations', []).append({
                'datasource': plan['datasource'],
                'relationship_index': plan['index'],
                'cardinality': 'manyToMany',
                'left_table': plan['left_table'],
                'right_table': plan['right_table'],
                'key_components': plan['key_components'],
                'filter_direction': filter_direction,
                'filter_from': plan['policy'].get('filter_from'),
                'nulls_match': plan['policy']['nulls_match'],
                'comparison_case_sensitive': plan['policy']['comparison_case_sensitive'],
                'comparison_exact': plan['policy']['comparison_exact'],
                'status': 'approximated',
                'note': (
                    'Power BI filters through an encoded composite many-to-many '
                    'relationship. Validate filter propagation, duplicate-key '
                    'aggregations, and measures against the Tableau workbook.'
                ),
            })


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
    from powerbi_import.errors import PartialGenerationError

    seen_rels = set()
    protected_pairs = {
        frozenset(pair) for pair in model.get('_logical_predicate_pairs', [])
    }
    compiled_equality_pairs = {
        frozenset(pair) for pair in model.get('_logical_equality_compiled_pairs', [])
    }

    def logical_pair_error(source_rel, endpoints, predicate=None):
        predicate = predicate or {}
        op = str(predicate.get('operator', '')).upper()
        if op == 'AND':
            leaves = predicate.get('arguments', [])
            pure_equality = bool(leaves) and all(
                str(leaf.get('operator', '')).upper() in ('=', '==')
                and all(
                    isinstance(leaf.get(side), dict)
                    and leaf[side].get('table') and leaf[side].get('column')
                    for side in ('left', 'right'))
                for leaf in leaves)
            blocker = (
                "the complete predicate has multiple equality terms and a "
                "single-column model relationship would discard composite-key "
                "semantics"
                if pure_equality else
                "the predicate contains a range, disjunction, opaque operand, "
                "or other non-equality term that a model relationship cannot encode"
            )
        elif op in ('=', '=='):
            blocker = (
                "source row uniqueness/cardinality and key encoding have not "
                "been proven for a model relationship"
            )
        else:
            blocker = (
                "the predicate is not a proven conjunctive equality; range, "
                "disjunctive, and ambiguous predicates remain unsupported"
            )
        left, right = endpoints
        edge = source_rel.get('left', {})
        other = source_rel.get('right', {})
        return (
            f"Cannot emit legacy equality edge {edge.get('table', left)}."
            f"{edge.get('column', '')} -> {other.get('table', right)}."
            f"{other.get('column', '')} for logical pair '{left}' / '{right}': "
            f"{blocker}. Preserve the complete source predicate or provide "
            "provable key/cardinality and encoding evidence."
        )

    def pure_equality_conjunction(predicate):
        terms = [predicate]
        if str(predicate.get('operator', '')).upper() == 'AND':
            terms = predicate.get('arguments', [])
        return bool(terms) and all(
            str(term.get('operator', '')).upper() in ('=', '==')
            and all(
                isinstance(term.get(side), dict)
                and term[side].get('table') and term[side].get('column')
                for side in ('left', 'right'))
            for term in terms)

    logical_predicates = {}
    for datasource in datasources:
        rename_map = model.get('_table_rename_map', {})
        for logical in (datasource.get('relation_metadata', {})
                        .get('logical_relationships', [])):
            left = logical.get('left', {}).get('tables', [])
            right = logical.get('right', {}).get('tables', [])
            if len(left) == len(right) == 1:
                pair = frozenset((
                    rename_map.get((datasource.get('name', ''), left[0]), left[0]),
                    rename_map.get((datasource.get('name', ''), right[0]), right[0]),
                ))
                logical_predicates.setdefault(pair, []).append(logical.get('predicate', {}))
                if logical.get('requires_materialization'):
                    protected_pairs.add(pair)

    for ds in datasources:
        relationships = []
        for source_rel in ds.get('relationships', []):
            # CLI fidelity preprocessing can add equality guesses *after*
            # extraction. Inspect their explicit provenance before
            # _build_relationships discards it. Never discard an unmarked
            # (declared) edge just because its endpoints look inferred.
            endpoints = [
                source_rel.get(side, {}).get('table')
                for side in ('left', 'right')
            ]
            rename_map = model.get('_table_rename_map', {})
            pair = frozenset(
                rename_map.get((ds.get('name', ''), name), name)
                for name in endpoints
            )
            if pair in protected_pairs:
                if source_rel.get('inferred') is True:
                    logger.info(
                        "Skipped generated inferred equality %s -> %s: "
                        "preserved logical predicate governs this pair",
                        *endpoints)
                    continue
                # Only a compiler that preserves the complete source predicate
                # may replace its legacy per-column projections.
                predicates = logical_predicates.get(pair, [])
                if (pair in compiled_equality_pairs and len(predicates) == 1
                        and pure_equality_conjunction(predicates[0])):
                    logger.info(
                        "Skipped lossy legacy equality projection for compiled "
                        "logical pair %s / %s", *endpoints)
                    continue
                raise PartialGenerationError(
                    logical_pair_error(
                        source_rel, endpoints,
                        predicates[0] if len(predicates) == 1 else {}),
                    source=ds.get('name'), recovery_kind='logical_relationship_predicate',
                    required_evidence=[
                        'Complete source predicate', 'Verified source key cardinality',
                        'Verified case and null-safe composite key encoding'])
            relationships.append(source_rel)
        rels = _build_relationships(relationships)
        for rel in rels:
            if frozenset((rel.get('fromTable'), rel.get('toTable'))) in protected_pairs:
                raise ValueError(
                    "Generated equality edge is blocked because it would replace "
                    "a preserved logical relationship predicate; composite, "
                    "range, ambiguous, and uncompiled predicates are never "
                    "projected into model relationships.")
            key = (rel.get('fromTable'), rel.get('fromColumn'),
                   rel.get('toTable'), rel.get('toColumn'))
            if key not in seen_rels:
                seen_rels.add(key)
                model["model"]["relationships"].append(rel)
            else:
                print(f"  ⚠ Skipped duplicate relationship: {key[0]}.{key[1]} → {key[2]}.{key[3]}")

    for relation in model.get('_materialized_composite_relationships', []):
        key = (relation.get('fromTable'), relation.get('fromColumn'),
               relation.get('toTable'), relation.get('toColumn'))
        if key in seen_rels:
            raise ValueError(f"Duplicate materialized composite relationship: {key}")
        seen_rels.add(key)
        model["model"]["relationships"].append(relation)

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
        # Source endpoints use internal names; table creation may have emitted
        # captions instead. Prefer its table-scoped evidence to name heuristics.
        produced = model.get('_column_rename_map', {}).get((table_name, col_name))
        if produced in available_cols:
            return produced
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


def _source_alias_pairs(model):
    """Identify aliases of the same physical relation within one datasource.

    Shared column names are not join provenance for these pairs: a Tableau
    logical self-relationship can use several predicates, including ranges.
    Require an explicit physical relation and connection identity; do not
    classify unrelated tables by their schemas or display names.
    """
    aliases = set()
    renames = model.get('_table_rename_map', {})

    def exact_hyper_identity(value):
        fields = ('workbook', 'archive_member', 'schema', 'table')
        return (
            isinstance(value, dict) and set(value) == set(fields)
            and all(isinstance(value.get(key), str) and value[key]
                    for key in fields)
        )

    for datasource in model.get('_datasources', []):
        ds_name = datasource.get('name', '')
        sources = []
        for table in datasource.get('tables', []):
            physical = table.get('source_table')
            connection = table.get('connection_details') or (
                datasource.get('connection_map', {}).get(table.get('connection'))
                or datasource.get('connection')
            )
            name = table.get('name')
            if not physical or not connection or not name:
                continue
            actual_name = renames.get((ds_name, name), name)
            identity = table.get('hyper_identity')
            connector = str(connection.get('type', '')).strip().casefold()
            is_hyper = connector in {'hyper', 'extract', 'dataengine'} or identity is not None
            for other_name, other_physical, other_connection, other_identity in sources:
                if is_hyper:
                    same_source = (
                        exact_hyper_identity(identity)
                        and exact_hyper_identity(other_identity)
                        and identity == other_identity
                    )
                else:
                    same_source = (
                        physical == other_physical
                        and connection == other_connection
                    )
                if actual_name != other_name and same_source:
                    aliases.add(frozenset((actual_name, other_name)))
            sources.append((actual_name, physical, connection, identity))
    return aliases


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
            if measure.get('_logical_relationship_pair_measure'):
                # Its iterators implement the complete source predicate.
                # A model equality edge would change that domain and must
                # not be inferred from these intentional cross-table refs.
                continue
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

    # Do not turn a missing logical self-relationship into a guessed equality
    # on RowID (or another coincidentally matching column).  In particular,
    # rewriting RELATED through that fabricated relationship into LOOKUPVALUE
    # can fail at refresh when a repeated key has several timestamps.  Moving
    # the calculation to the wider/narrower alias does not recover the missing
    # predicates either.  Stop before mutating relationships, not with blank
    # columns or an arbitrary aggregation that hides the lost row grain.
    source_aliases = _source_alias_pairs(model) | {
        frozenset(pair) for pair in model.get('_logical_predicate_pairs', [])
    }
    unresolved_aliases = {
        tuple(sorted((source_table, ref_table)))
        for source_table, ref_table in needed_pairs
        if (source_table, ref_table) not in connected_pairs
        and frozenset((source_table, ref_table)) in source_aliases
    }
    if unresolved_aliases:
        pairs = '; '.join(
            f"'{left}' / '{right}'" for left, right in sorted(unresolved_aliases)
        )
        raise ValueError(
            "Cannot infer a relationship between physical source aliases "
            f"{pairs}: cross-table calculations require the original Tableau "
            "relationship predicates, but no explicit relationship connects "
            "these tables in the extracted model. Column-name or RowID "
            "matching cannot preserve composite/range self-relationship "
            "semantics. Preserve the source predicates in extraction and "
            "implement their row grain before generating this semantic model."
        )

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
            # Even without a current DAX dependency, aliases are not evidence
            # of an equality relationship.  Leave them disconnected rather
            # than proactively fabricating a key that later rewrites can use.
            if frozenset((t1, t2)) in source_aliases:
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

    for rel in model['model']['relationships']:
        if rel.pop('_explicit_composite_cardinality', False):
            continue
        if rel.pop('_source_cardinality_verified', False):
            rel['fromCardinality'] = 'many'
            rel['toCardinality'] = 'one'
            rel['crossFilteringBehavior'] = 'oneDirection'
            continue
        to_table = rel.get('toTable', '')
        to_col = rel.get('toColumn', '')
        from_table = rel.get('fromTable', '')
        join_type = rel.get('joinType', 'left')

        if to_table == 'Calendar':
            # We generate Calendar ourselves, one row per date, so Date is
            # unique as a matter of fact. Every branch below is a guess about
            # uniqueness, and a guess must not overrule something known --
            # the column-count heuristic used to read our own date table as a
            # peer table and make it many-to-many.
            rel['fromCardinality'] = 'many'
            rel['toCardinality'] = 'one'
            rel['crossFilteringBehavior'] = 'oneDirection'
            print(f"  ✓  Relation → '{to_table}.{to_col}' set to manyToOne (Calendar table, single direction).")
        elif join_type == 'full':
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
    Preserve text identifiers; for purely numeric keys align the toColumn type.
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

        if to_type.lower() == 'string' and from_type.lower() != 'string':
            target_table, target_col, target_name = from_table, from_col, from_col_name
            desired_type = to_type
        else:
            target_table, target_col, target_name = to_table, to_col, to_col_name
            desired_type = from_type

        print(f"  \u26a0\ufe0f  Type mismatch: {rel.get('fromTable')}.{from_col_name} ({from_type}) "
              f"-> {rel.get('toTable')}.{to_col_name} ({to_type}). Aligning to {desired_type}.")

        old_type = target_col.get('dataType', 'string')
        target_col['dataType'] = desired_type

        if desired_type.lower() == 'string':
            target_col['summarizeBy'] = 'none'
            target_col.pop('formatString', None)

        old_m_type = pbi_to_m.get(old_type, '')
        new_m_type = pbi_to_m.get(desired_type, '')
        if old_m_type and new_m_type:
            for partition in target_table.get('partitions', []):
                source = partition.get('source', {})
                if isinstance(source, dict) and 'expression' in source:
                    expr = source['expression']
                    old_pattern = f'"{target_name}", {old_m_type}'
                    new_pattern = f'"{target_name}", {new_m_type}'
                    if old_pattern in expr:
                        source['expression'] = expr.replace(old_pattern, new_pattern)
        else:
            print(f"    \u26a0\ufe0f  Cannot map M types for {target_name}: {repr(old_type)} / {repr(desired_type)}")


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
        if rel.get('isActive') is False:
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
