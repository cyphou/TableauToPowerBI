"""
Datasource extraction module for Tableau workbooks.

Parses Tableau XML datasource elements, extracting connections,
tables, columns, calculations, and relationships.
Re-exports DAX converter and M query builder functions for backward compatibility.
"""

import xml.etree.ElementTree as ET
import zipfile
import os
import csv
import re
import logging
import copy
from dax_converter import _reverse_tableau_bracket_escape
try:
    from .safe_xml import safe_findall, safe_findtext, safe_get_attr
    from .extract_primitives import _extract_calculation_formula
except ImportError:
    from safe_xml import safe_findall, safe_findtext, safe_get_attr
    from extract_primitives import _extract_calculation_formula

logger = logging.getLogger(__name__)

def _detect_csv_delimiter(header_line):
    """Detects the CSV delimiter from the first line (header).
    
    Uses csv.Sniffer if possible, otherwise heuristic by counting.
    Returns the detected delimiter (',' or ';' or '\t' etc.)
    """
    if not header_line:
        return ','
    
    # Try csv.Sniffer first
    try:
        dialect = csv.Sniffer().sniff(header_line, delimiters=',;\t|')
        return dialect.delimiter
    except csv.Error:
        pass
    
    # Heuristic: count occurrences of common delimiters
    candidates = [(',', header_line.count(',')),
                  (';', header_line.count(';')),
                  ('\t', header_line.count('\t')),
                  ('|', header_line.count('|'))]
    # Sort by descending occurrence count
    candidates.sort(key=lambda x: x[1], reverse=True)
    if candidates[0][1] > 0:
        return candidates[0][0]
    return ','


def _read_csv_header_from_twbx(twbx_path, directory, filename):
    """Reads the first line of a CSV file embedded in a .twbx.
    
    Returns the first line (header) or None if not found.
    """
    if not twbx_path or not os.path.exists(twbx_path):
        return None
    ext = os.path.splitext(twbx_path)[1].lower()
    if ext not in ('.twbx', '.tdsx'):
        return None
    
    # Build the expected path inside the archive
    if directory:
        csv_path = directory.rstrip('/') + '/' + filename
    else:
        csv_path = filename
    
    try:
        with zipfile.ZipFile(twbx_path, 'r') as z:
            # Search for the file (exact or partial match)
            for name in z.namelist():
                if name == csv_path or name.endswith('/' + filename):
                    with z.open(name) as f:
                        first_line = f.readline().decode('utf-8', errors='replace').strip()
                        return first_line
    except (zipfile.BadZipFile, OSError) as e:
        logger.debug('Could not read CSV from archive %s: %s', twbx_path, e)
    return None


def _extract_col_local_name_map(datasource_elem):
    """Build mapping of Tableau local-names to their parent table names.

    Uses ``<metadata-record class="column">`` elements which contain the
    authoritative local-name → parent-name association.  This captures
    columns that may not have a ``<column>`` element at the datasource
    level (e.g. Salesforce Id, Probability fields) but are referenced by
    calculations.

    Returns:
        dict mapping column local-name (without brackets) to parent table
        name, e.g. ``{'Opportunity ID': 'Opportunities'}``.
    """
    if datasource_elem is None:
        return {}

    result = {}
    for mr in safe_findall(datasource_elem, './/metadata-record[@class="column"]'):
        local_name = (safe_findtext(mr, 'local-name', '') or '').strip().strip('[]')
        parent_name = (safe_findtext(mr, 'parent-name', '') or '').strip().strip('[]')
        if local_name and parent_name and local_name not in result:
            result[local_name] = parent_name
    return result


def _extract_col_type_map(datasource_elem):
    """Build mapping of column local-names to their data types.

    Uses ``<metadata-record class="column">`` ``local-type`` which is the
    authoritative type for physical columns that may lack a ``<column>``
    element (e.g. Salesforce ``Probability``).

    Returns:
        dict mapping column local-name (without brackets) to Tableau
        datatype string, e.g. ``{'Probability (%)': 'real'}``.
    """
    if datasource_elem is None:
        return {}

    result = {}
    for mr in safe_findall(datasource_elem, './/metadata-record[@class="column"]'):
        local_name = (safe_findtext(mr, 'local-name', '') or '').strip().strip('[]')
        local_type = (safe_findtext(mr, 'local-type', '') or '').strip()
        if local_name and local_type and local_name not in result:
            result[local_name] = local_type
    return result


def extract_datasource(datasource_elem, twbx_path=None):
    """
    Extracts the full details of a Tableau datasource
    
    Args:
        datasource_elem: XML element of the datasource
        twbx_path: Original workbook/datasource path (archive delimiter detection
            or loose native-file source provenance)
    
    Returns:
        dict with connection, tables, columns, calculations, relationships
    """
    if datasource_elem is None:
        return {
            'name': 'Unknown',
            'caption': 'Unknown',
            'connection': {'type': 'Unknown', 'details': {}},
            'connection_map': {},
            'tables': [],
            'calculations': [],
            'columns': [],
            'relationships': [],
            'relation_metadata': {'version': 1, 'physical_relations': [],
                                  'logical_tables': [], 'logical_relationships': []},
            'col_local_name_map': {},
            'col_type_map': {},
        }

    original_elem = datasource_elem
    datasource_elem, inactive_context = _native_datasource_context(datasource_elem)
    ds_name = safe_get_attr(datasource_elem, 'name', 'Unknown')
    ds_caption = safe_get_attr(datasource_elem, 'caption', ds_name)
    
    # Build the connection_name -> connection_details mapping
    connection_map = _build_connection_map(datasource_elem, twbx_path=twbx_path)
    
    calcs = extract_calculations(datasource_elem)
    for c in calcs:
        c['datasource_name'] = ds_name

    relation_metadata = extract_relation_metadata(datasource_elem)
    datasource = {
        'name': ds_name,
        'caption': ds_caption,
        'connection': extract_connection_details(datasource_elem, twbx_path=twbx_path),
        'connection_map': connection_map,
        'tables': extract_tables_with_columns(datasource_elem, connection_map),
        'calculations': calcs,
        'columns': extract_column_metadata(datasource_elem),
        'relationships': extract_relationships(datasource_elem, relation_metadata),
        'relation_metadata': relation_metadata,
        'col_local_name_map': _extract_col_local_name_map(datasource_elem),
        'col_type_map': _extract_col_type_map(datasource_elem),
    }
    # Direct native connections have no named map entry to share with tables.
    if (not connection_map
            and not datasource_elem.findall('.//named-connection')
            and datasource['connection']['details'].get('_source_base_dir')):
        for table in datasource['tables']:
            if not table['connection'] and not table['connection_details']:
                table['connection_details'] = datasource['connection']

    # Keep published identity alongside datasources.json. The separate global
    # published-datasource list is not available to --prep's binding planner.
    repositories = [
        elem for elem in original_elem.iter()
        if isinstance(elem.tag, str)
        and elem.tag.rsplit('}', 1)[-1].rsplit(':', 1)[-1] == 'repository-location'
    ]
    if len(repositories) == 1:
        repo = repositories[0]
        datasource['published_datasource'] = {
            'name': repo.get('name') or repo.get('id', ''),
            'id': repo.get('id', ''),
            'path': repo.get('path', ''),
            'site': repo.get('site', ''),
        }
    if inactive_context:
        datasource['inactive_extract_context'] = inactive_context

    materialized = (_bind_packaged_hyper_identities(
        original_elem, datasource['tables'], twbx_path)
        if not inactive_context else None)
    if materialized is not None:
        # Native joins/unions and extract filters have already been evaluated
        # by Tableau. Keep them as evidence, never as executable model edges.
        materialized['native_relation_metadata'] = datasource['relation_metadata']
        materialized['native_relationships'] = datasource['relationships']
        materialized['native_col_local_name_map'] = datasource['col_local_name_map']
        datasource['materialized_extract'] = materialized
        table = datasource['tables'][0]
        name = table['name']
        datasource['relationships'] = []
        relation = {'type': 'table', 'name': name, 'connection': '',
                    'source_table': '[{}].[{}]'.format(
                        table['hyper_identity']['schema'], table['hyper_identity']['table'])}
        datasource['relation_metadata'] = {
            'version': 1, 'physical_relations': [relation],
            'logical_tables': [{'object_id': materialized['object_id'],
                                'caption': name, 'relations': [dict(relation)]}],
            'logical_relationships': [],
        }
        datasource['col_local_name_map'] = {
            c['name']: name for c in table['columns']}
        # Existing datasource-scoped calculation routing now has exactly one
        # host. Do not stamp a raw table name: the model may rename collisions.

    # Rename 'sqlproxy' tables to use the datasource caption.
    # Published datasources (hosted on Tableau Server) expose a single relation
    # named literally 'sqlproxy' — an internal Tableau class token, not a
    # user-facing name. Without renaming, PBI would surface tables called
    # 'sqlproxy' or 'sqlproxy (Caption)' which is meaningless to end users.
    # The friendly name lives on the parent <datasource caption="...">.
    _rename_sqlproxy_tables(datasource)

    # Ensure join columns referenced by relationships exist in their tables.
    # Connectors like Salesforce use internal primary keys (e.g. Id) for joins
    # that Tableau doesn't expose as visible columns.
    _ensure_relationship_columns(datasource)

    # Ensure columns referenced by calculations exist in their parent tables.
    # Salesforce connectors may have renamed columns (e.g. Id → "Opportunity ID")
    # that appear in <metadata-record> elements and <cols><map> but have no
    # <column> element, so they are not extracted by Phase 2.
    _ensure_calc_referenced_columns(datasource)
    
    return datasource


def _extract_context_tag(elem):
    """Normalize only the known active object-model feature gate."""
    local = elem.tag.rsplit('}', 1)[-1]
    for feature in ('ObjectModelEncapsulateLegacy', 'SpatialFileUnion'):
        prefix = '_.fcp.' + feature + '.'
        if local.startswith(prefix + 'false...'):
            return None
        if local.startswith(prefix + 'true...'):
            return local[len(prefix + 'true...'):]
    return local


def _active_extract_elements(elem):
    if _extract_context_tag(elem) is not None:
        yield elem
        for child in elem:
            yield from _active_extract_elements(child)


def _native_datasource_context(datasource_elem, force=False):
    """Exclude explicitly inactive extract scopes, without mutating source XML.

    No extract declaration means native operation; an explicit single disabled
    extract does too. Missing/unknown enabled flags and multiple declarations
    are deliberately not interpreted as native activation evidence.
    ``force`` is for native relation provenance, not a model binding policy.
    """
    extracts = [e for e in datasource_elem if _extract_context_tag(e) == 'extract']
    native = (not extracts or
              (len(extracts) == 1 and extracts[0].get('enabled', '').lower() == 'false'))
    contexts = [e for e in _active_extract_elements(datasource_elem)
                if _extract_context_tag(e) == 'properties' and e.get('context') == 'extract']
    if not force and (not native or (not extracts and not contexts)):
        return datasource_elem, None
    provenance = {
        'selection': 'native_no_enabled_extract',
        'extract_xml': [ET.tostring(e, encoding='unicode') for e in extracts],
        'object_extract_contexts_xml': [ET.tostring(e, encoding='unicode') for e in contexts],
    }
    selected = copy.deepcopy(datasource_elem)

    def prune(parent):
        for child in list(parent):
            tag = _extract_context_tag(child)
            if (tag is None or tag == 'extract'
                    or (tag == 'properties' and child.get('context') == 'extract')):
                parent.remove(child)
            else:
                # Existing ElementTree helpers use literal unqualified tags.
                child.tag = tag
                prune(child)
    prune(selected)
    return selected, provenance


def _bind_packaged_hyper_identities(datasource_elem, tables, twbx_path):
    """Bind explicit extract relations to exact packaged members, without guesses.

    This records XML-declared identities, not proof of a successful export.
    Consumers must join all four keys to the catalog-backed export manifest.
    Ambiguous deduplicated relation names deliberately receive no identity.
    """
    if not twbx_path or os.path.splitext(twbx_path)[1].lower() not in ('.twbx', '.tdsx'):
        return
    try:
        with zipfile.ZipFile(twbx_path) as archive:
            members = archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return

    # A relational native context may only bind as a complete materialized
    # object. Never bind its staging alias while leaving native leaves active,
    # including when the strict object-context proof fails.
    relational_context = any(
        _extract_context_tag(e) == 'properties' and e.get('context') == ''
        and any(_extract_context_tag(r) == 'relation'
                and r.get('type') != 'table' for r in _active_extract_elements(e))
        for e in _active_extract_elements(datasource_elem))
    if relational_context:
        return _bind_object_context_extract(datasource_elem, tables, twbx_path, members)

    # Match bracketed components as units: dots inside identifiers are literal.
    qualified = re.compile(r'\[((?:[^\]]|\]\])*)\]\.\[((?:[^\]]|\]\])*)\]')
    declarations = {}

    def visit(elem, in_extract=False, connection=None, ancestors=()):
        tag = _extract_context_tag(elem)
        if tag is None:
            return
        if tag == 'extract':
            in_extract = True
            connection = None
        elif tag == 'connection':
            connection = elem
        if tag == 'relation' and elem.get('type', '') == 'table':
            identity = None
            match = qualified.fullmatch(elem.get('table', ''))
            parts = tuple(part.replace(']]', ']') for part in match.groups()) if match else None
            if (in_extract and connection is not None
                    and connection.get('class', '') == 'hyper'
                    and (not elem.get('connection', '')
                         or elem.get('connection') == connection.get('name'))):
                member = connection.get('dbname', '')
                if (member.lower().endswith('.hyper') and members.count(member) == 1
                        and parts is not None and all(parts)):
                    schema, table = parts
                    identity = {
                        'workbook': os.path.abspath(twbx_path),
                        'archive_member': member,
                        'schema': schema,
                        'table': table,
                    }
            # Object-graph properties may repeat the exact extract relation
            # outside a connection. They are references, never binding origins.
            reference = (not in_extract and connection is None
                         and not elem.get('connection', '')
                         and 'object' in ancestors and 'properties' in ancestors)
            declarations.setdefault(elem.get('name', ''), []).append((identity, parts, reference))
        for child in elem:
            visit(child, in_extract, connection, ancestors + (tag,))

    visit(datasource_elem)
    for table in tables:
        candidates = declarations.get(table.get('name', ''), [])
        identities = [identity for identity, _, _ in candidates if identity is not None]
        if not identities:
            continue
        identity = identities[0]
        if all(candidate == identity or (
                candidate is None and reference
                and parts == (identity['schema'], identity['table']))
                for candidate, parts, reference in candidates):
            table['hyper_identity'] = dict(identity)

    return _bind_object_context_extract(datasource_elem, tables, twbx_path, members)


def _bind_object_context_extract(datasource_elem, tables, twbx_path, members):
    """Prefer a single explicitly paired, active packaged extract logical object.

    Unlike the direct-relation path above, this requires connection-level
    schema/tablename and both object contexts. No names, columns, or captions
    are used to guess an association. A relational native context requires
    complete field lineage before replacing the whole object by its extract.
    """
    def tag(elem):
        return _extract_context_tag(elem)

    def children(elem, name):
        return [child for child in elem if tag(child) == name]

    qualified = re.compile(r'\[((?:[^\]]|\]\])*)\]\.\[((?:[^\]]|\]\])*)\]')

    def parts(value):
        match = qualified.fullmatch(value)
        return tuple(p.replace(']]', ']') for p in match.groups()) if match else None

    extracts = children(datasource_elem, 'extract')
    if len(extracts) != 1 or extracts[0].get('enabled', '').lower() != 'true':
        return
    extract = extracts[0]
    connections = [e for e in _active_extract_elements(extract) if tag(e) == 'connection']
    if len(connections) != 1 or connections[0] not in list(extract):
        return
    connection = connections[0]
    member = connection.get('dbname', '')
    schema, physical_name = connection.get('schema', ''), connection.get('tablename', '')
    if (connection.get('class') != 'hyper' or not schema or not physical_name
            or not member.lower().endswith('.hyper') or members.count(member) != 1):
        return

    graphs = [e for e in _active_extract_elements(datasource_elem) if tag(e) == 'object-graph']
    if len(graphs) != 1 or graphs[0] not in list(datasource_elem):
        return
    if graphs[0].tag.rsplit('}', 1)[-1].startswith(
            '_.fcp.ObjectModelEncapsulateLegacy.false...'):
        return
    objects_nodes = children(graphs[0], 'objects')
    if len(objects_nodes) != 1:
        return
    objects = children(objects_nodes[0], 'object')
    if len(objects) != 1:
        return
    obj = objects[0]
    if not obj.get('id'):
        return
    properties = children(obj, 'properties')
    if (len(properties) != 2
            or sorted(p.get('context', '') for p in properties) != ['', 'extract']
            or any('context' not in p.attrib for p in properties)):
        return
    native_props = next(p for p in properties if p.get('context') == '')
    extract_props = next(p for p in properties if p.get('context') == 'extract')

    def sole_relation(props):
        relations = [e for e in _active_extract_elements(props) if tag(e) == 'relation']
        if (len(relations) != 1 or relations[0] not in list(props)
                or relations[0].get('type') != 'table'):
            return None
        return relations[0]

    native, physical = sole_relation(native_props), sole_relation(extract_props)
    if native is None and physical is not None:
        if any(tag(e) == 'relationship' or '...' in (tag(e) or '')
               for e in _active_extract_elements(graphs[0])):
            return
        return _bind_materialized_object_extract(
            datasource_elem, tables, twbx_path, extract, connection,
            obj, native_props, physical)
    if native is None or physical is None:
        return
    native_name, alias = native.get('name', ''), physical.get('name', '')
    native_ref = native.get('connection', '')
    if (not native_name or not alias or native_name == alias or not native_ref
            or not native.get('table') or physical.get('connection', '')
            or parts(physical.get('table', '')) != (schema, physical_name)):
        return

    roots = children(datasource_elem, 'connection')
    if len(roots) != 1:
        return
    root = roots[0]
    named = [e for e in _active_extract_elements(root)
             if tag(e) == 'named-connection' and e.get('name') == native_ref]
    if len(named) != 1 or len(children(named[0], 'connection')) != 1:
        return
    if not children(named[0], 'connection')[0].get('class'):
        return

    # Deduplication by relation name has already happened. Prove that every
    # declaration of either name is the SAME source in an allowed location
    # before mutating or removing any table. Matching aliases alone is unsafe.
    parents = {child: parent for parent in datasource_elem.iter() for child in parent}
    for relation in (e for e in _active_extract_elements(datasource_elem)
                     if tag(e) == 'relation'):
        if relation.get('type') != 'table':
            return  # joins, unions, custom SQL, or other unsupported shapes
        name = relation.get('name', '')
        parent = parents.get(relation)
        if name == native_name:
            if (parent not in (root, native_props)
                    or relation.get('connection', '') != native_ref
                    or relation.get('table', '') != native.get('table')):
                return
        elif name == alias:
            if (parent not in (connection, extract_props)
                    or relation.get('connection', '')
                    or parts(relation.get('table', '')) != (schema, physical_name)):
                return
        else:
            return  # multi-table context is not supported by this bounded path

    logical_tables = [t for t in tables if t.get('name') == native_name]
    physical_tables = [t for t in tables if t.get('name') == alias]
    if len(logical_tables) != 1 or len(physical_tables) > 1:
        return
    logical = logical_tables[0]
    identity = {
        'workbook': os.path.abspath(twbx_path),
        'archive_member': member,
        'schema': schema,
        'table': physical_name,
    }
    if (logical.get('hyper_identity') is not None
            and logical['hyper_identity'] != identity):
        return
    column_map = dict(logical.get('cols_physical_map') or {})
    for table in physical_tables:
        if table.get('hyper_identity') is not None and table['hyper_identity'] != identity:
            return
        for remote, local in (table.get('cols_physical_map') or {}).items():
            if remote in column_map and column_map[remote] != local:
                return
            column_map[remote] = local
    # Preserve logical routing, columns, native metadata and calculation names.
    # Only the explicit physical identity and its staging rename map move.
    logical['hyper_identity'] = identity
    if column_map:
        logical['cols_physical_map'] = column_map
    tables[:] = [t for t in tables if not any(t is p for p in physical_tables)]


def _bind_materialized_object_extract(datasource_elem, tables, twbx_path,
                                      extract, connection, obj, native_props, physical):
    """Collapse a proven single relational object, atomically and fail closed.

    XML metadata proves field identity, not successful Hyper export. Consumers
    must still validate the exact identity against their catalog/export manifest.
    No catalog is opened or inferred from filenames by this extraction path.
    """
    tag = _extract_context_tag

    def children(elem, name):
        return [c for c in elem if tag(c) == name]

    def shape(elem):
        return (tag(elem), tuple(sorted(elem.attrib.items())),
                (elem.text or '').strip(),
                tuple(shape(c) for c in elem if tag(c) is not None))

    def identifier(value):
        match = re.fullmatch(r'\[((?:[^\]]|\]\])*)\]', value)
        return match.group(1).replace(']]', ']') if match else None

    def qualified(value):
        match = re.fullmatch(
            r'\[((?:[^\]]|\]\])*)\]\.\[((?:[^\]]|\]\])*)\]', value)
        return tuple(p.replace(']]', ']') for p in match.groups()) if match else None

    roots = children(datasource_elem, 'connection')
    native_relations = children(native_props, 'relation')
    if len(roots) != 1 or len(native_relations) != 1:
        return
    root, native = roots[0], native_relations[0]
    if native.get('type') not in ('join', 'union', 'batch-union'):
        return
    source_relations = children(root, 'relation')
    if len(source_relations) != 1 or shape(source_relations[0]) != shape(native):
        return
    schema, physical_name = connection.get('schema'), connection.get('tablename')
    alias = physical.get('name', '')
    name = obj.get('caption', '')
    if (not name or not alias or physical.get('connection')
            or qualified(physical.get('table', '')) != (schema, physical_name)):
        return
    origins = children(connection, 'relation')
    if (len(origins) != 1 or origins[0].get('type') != 'table'
            or origins[0].get('name') != alias or origins[0].get('connection')
            or qualified(origins[0].get('table', '')) != (schema, physical_name)):
        return

    active_native = list(_active_extract_elements(native))
    aliases = set()
    for relation in active_native:
        if '...' in (tag(relation) or ''):
            return  # unknown feature policy
        if tag(relation) != 'relation':
            continue
        kind = relation.get('type')
        nested = children(relation, 'relation')
        if kind not in ('table', 'join', 'union', 'batch-union'):
            return  # custom SQL or unknown native semantics
        if kind == 'join' and (len(nested) != 2 or not relation.get('join')):
            return
        if kind == 'union' and len(nested) < 2:
            return
        if kind in ('table', 'batch-union'):
            ref, source_alias = relation.get('connection'), relation.get('name')
            named = [e for e in _active_extract_elements(root)
                     if tag(e) == 'named-connection' and e.get('name') == ref]
            if (not ref or not source_alias or source_alias in aliases
                    or len(named) != 1 or len(children(named[0], 'connection')) != 1
                    or not children(named[0], 'connection')[0].get('class')):
                return
            if kind == 'table' and (nested or not relation.get('table')):
                return
            aliases.add(source_alias)
    # No independent relations may be silently deleted by the collapse.
    allowed = set()
    for subtree in (source_relations[0], native, origins[0], physical):
        allowed.update(e for e in _active_extract_elements(subtree) if tag(e) == 'relation')
    if any(tag(e) == 'relation' and e not in allowed
           for e in _active_extract_elements(datasource_elem)):
        return
    if (any(t.get('name') not in aliases | {alias} for t in tables)
            or len([t for t in tables if t.get('name') == alias]) != 1):
        return

    records_nodes = children(connection, 'metadata-records')
    if len(records_nodes) != 1:
        return
    records = [e for e in records_nodes[0]
               if tag(e) == 'metadata-record' and e.get('class') == 'column']
    if not records:
        return
    mapping, columns, local_names = {}, [], set()
    ds_columns = {identifier(c.get('name', '')): c
                  for c in children(datasource_elem, 'column')}
    for record in records:
        def text(key):
            values = children(record, key)
            return (values[0].text or '').strip() if len(values) == 1 else ''
        remote, local, dtype = text('remote-name'), identifier(text('local-name')), text('local-type')
        if (not remote or not local
                or dtype not in ('string', 'integer', 'real', 'boolean', 'date', 'datetime', 'spatial')
                or identifier(text('parent-name')) != alias
                or remote in mapping or local in local_names):
            return
        mapping[remote] = local
        local_names.add(local)
        definition = ds_columns.get(local)
        role = (definition.get('role') if definition is not None else None)
        column = {
            'name': local, 'datatype': dtype,
            'role': role or ('measure' if dtype in ('integer', 'real') else 'dimension'),
        }
        columns.append(column)
    # If explicit physical renames exist they must exactly corroborate metadata,
    # not merely cover a convenient subset (Fusac has collision-sensitive names).
    cols = children(connection, 'cols')
    if len(cols) > 1:
        return
    if cols:
        explicit = {}
        for entry in cols[0]:
            pair, local = qualified(entry.get('value', '')), identifier(entry.get('key', ''))
            if (tag(entry) != 'map' or pair is None or pair[0] != alias or not local
                    or pair[1] in explicit):
                return
            explicit[pair[1]] = local
        if explicit != mapping:
            return
    native_names = set()
    for record in _active_extract_elements(root):
        if tag(record) != 'metadata-record' or record.get('class') != 'column':
            continue
        local_nodes = children(record, 'local-name')
        parent_nodes = children(record, 'parent-name')
        remote_nodes = children(record, 'remote-name')
        if (len(local_nodes) != 1 or len(parent_nodes) != 1 or len(remote_nodes) != 1
                or not (remote_nodes[0].text or '').strip()
                or identifier(parent_nodes[0].text or '') not in aliases):
            return
        local = identifier(local_nodes[0].text or '')
        if local not in local_names or local in native_names:
            return
        native_names.add(local)
    if native_names != local_names:
        return
    for local, definition in ds_columns.items():
        if (children(definition, 'calculation') or (local or '').startswith(':')):
            continue
        if not local or local not in local_names:
            return
    # Check already available declared/export column metadata too. Missing or
    # enriched staging fields must not silently stand in for extract metadata.
    staging = next(t for t in tables if t.get('name') == alias)
    staging_names = [c.get('name') for c in staging.get('columns', [])]
    if len(staging_names) != len(local_names) or set(staging_names) != local_names:
        return
    staging_columns = {c['name']: c for c in staging['columns']}
    columns = [dict(staging_columns[c['name']], **c) for c in columns]
    existing_map = staging.get('cols_physical_map') or {}
    if any(mapping.get(remote) != local for remote, local in existing_map.items()):
        return
    identity = {
        'workbook': os.path.abspath(twbx_path),
        'archive_member': connection.get('dbname'),
        'schema': schema, 'table': physical_name,
    }
    if any(t.get('hyper_identity') not in (None, identity) for t in tables):
        return
    provenance = {
        'object_id': obj.get('id'),
        'binding': 'single_object_materialized_extract',
        'field_mapping_validation': 'exact_declared_extract_metadata',
        'catalog_validation': 'required_at_export',
        'native_tables': tables[:],
        'native_context_xml': ET.tostring(native_props, encoding='unicode'),
        'extract_filters_xml': [
            ET.tostring(e, encoding='unicode')
            for e in _active_extract_elements(extract) if tag(e) == 'filter'],
        'spatial_limitations': 'Catalog identity does not certify spatial export or visual support.',
    }
    flattened = {
        'name': name, 'caption': name, 'type': 'table',
        'columns': columns, 'connection': '',
        'connection_details': {'type': 'hyper', 'details': {}},
        'source_table': physical.get('table'),
        'hyper_identity': identity, 'cols_physical_map': mapping,
    }
    tables[:] = [flattened]
    return provenance


def _sanitize_caption_for_table_name(caption):
    """Strip Tableau bracket-escaping and surrounding whitespace from a caption
    so it can be used as a TMDL-friendly table name.
    """
    if not caption:
        return ''
    # Tableau XML occasionally stores names wrapped in [brackets]; strip them
    # (TMDL adds its own quoting for names containing spaces / special chars).
    clean = caption.strip()
    if clean.startswith('[') and clean.endswith(']'):
        clean = clean[1:-1]
    return clean.strip()


def _rename_sqlproxy_tables(datasource):
    """Replace 'sqlproxy' table names with the datasource caption.

    For published datasources, Tableau emits `<relation name="sqlproxy">` as
    the single physical table. The friendly name lives on the parent
    `<datasource caption="...">`. Without this rewrite the user sees a table
    literally called `sqlproxy` (or `sqlproxy (Caption)` after the downstream
    collision-deduplication) — both meaningless. We rename to the caption,
    falling back to a stripped datasource id if no caption is available.
    """
    tables = datasource.get('tables', [])
    if not tables:
        return

    ds_caption = datasource.get('caption', '') or ''
    ds_name = datasource.get('name', '') or ''
    pretty = _sanitize_caption_for_table_name(ds_caption)
    if not pretty or pretty == ds_name:
        # Caption missing or identical to the opaque ds id — derive a label
        # from the ds id by dropping the 'sqlproxy.' / 'federated.' prefix.
        candidate = ds_name
        for prefix in ('sqlproxy.', 'federated.'):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        pretty = candidate or 'Published Datasource'

    rename_map = {}  # old_name -> new_name
    for table in tables:
        old_name = table.get('name', '')
        if old_name != 'sqlproxy':
            continue
        # Avoid collisions inside the same datasource (extremely rare; published
        # datasources expose exactly one sqlproxy relation, but defend anyway).
        candidate = pretty
        counter = 2
        existing = {t.get('name', '') for t in tables if t is not table}
        while candidate in existing:
            candidate = f"{pretty} ({counter})"
            counter += 1
        table['name'] = candidate
        rename_map[old_name] = candidate

    if not rename_map:
        return

    # Propagate the rename to relationships and calculations that reference
    # the old 'sqlproxy' table name.
    for rel in datasource.get('relationships', []):
        for side in ('left', 'right'):
            info = rel.get(side, {})
            tname = info.get('table', '')
            if tname in rename_map:
                info['table'] = rename_map[tname]

    for calc in datasource.get('calculations', []):
        tname = calc.get('column_table')
        if tname in rename_map:
            calc['column_table'] = rename_map[tname]


def _ensure_relationship_columns(datasource):
    """Add missing join columns to tables so relationships can be validated."""
    tables = datasource.get('tables', [])
    rels = datasource.get('relationships', [])
    if not tables or not rels:
        return

    table_map = {}
    for t in tables:
        tname = t.get('name', '')
        col_names = {c.get('name', '') for c in t.get('columns', [])}
        table_map[tname] = (t, col_names)

    for rel in rels:
        for side in ('left', 'right'):
            info = rel.get(side, {})
            tname = info.get('table', '')
            col = info.get('column', '')
            if not tname or not col or tname not in table_map:
                continue
            table_obj, col_names = table_map[tname]
            if col in col_names:
                continue
            # Check with table suffix (Salesforce pattern: 'Id' → 'Id (TableName)')
            suffixed = f"{col} ({tname})"
            if suffixed in col_names:
                # Update relationship to use the actual suffixed column name
                info['column'] = suffixed
                continue
            # Column truly missing — add it as a hidden key column
            table_obj.setdefault('columns', []).append({
                'name': col,
                'datatype': 'string',
                'role': 'dimension',
                'type': 'nominal',
                'hidden': True,
            })
            table_map[tname] = (table_obj, col_names | {col})


def _ensure_calc_referenced_columns(datasource):
    """Add missing columns to tables when calculations reference them.

    Salesforce and similar connectors rename physical columns via
    ``<metadata-record>`` (e.g. ``Id`` → ``Opportunity ID``).  These
    renamed columns often have no ``<column>`` element at the datasource
    level, so they are skipped during Phase 2 extraction.  When a
    calculation formula references such a column and
    ``col_local_name_map`` maps it to a specific table, this function
    adds the column to that table so the downstream TMDL generator can
    emit valid DAX (LOOKUPVALUE / RELATED).
    """
    tables = datasource.get('tables', [])
    calcs = datasource.get('calculations', [])
    col_map = datasource.get('col_local_name_map', {})
    col_type_map = datasource.get('col_type_map', {})
    if not tables or not calcs or not col_map:
        return

    # Build table lookup
    table_lookup = {}
    for t in tables:
        tname = t.get('name', '')
        col_names = {c.get('name', '') for c in t.get('columns', [])}
        table_lookup[tname] = (t, col_names)

    # Collect all column names referenced in calc formulas
    referenced_cols = set()
    for calc in calcs:
        formula = calc.get('formula', '')
        if formula:
            referenced_cols.update(re.findall(r'\[([^\]]+)\]', formula))

    # Add missing columns to their parent table
    for col_name in referenced_cols:
        parent_table = col_map.get(col_name)
        if not parent_table or parent_table not in table_lookup:
            continue
        table_obj, col_names = table_lookup[parent_table]
        if col_name in col_names:
            continue
        # Check with table suffix
        suffixed = f"{col_name} ({parent_table})"
        if suffixed in col_names:
            continue
        # Use authoritative type from metadata-records when available
        dtype = col_type_map.get(col_name, 'string')
        role = 'measure' if dtype in ('real', 'integer') else 'dimension'
        table_obj.setdefault('columns', []).append({
            'name': col_name,
            'datatype': dtype,
            'role': role,
            'type': 'quantitative' if dtype in ('real', 'integer') else 'nominal',
            'hidden': True,
        })
        table_lookup[parent_table] = (table_obj, col_names | {col_name})


def enrich_datasource_from_hyper(datasource, hyper_tables):
    """Enrich datasource metadata using data read from ``.hyper`` files.

    Bridges the gap between ``hyper_reader`` output and the datasource dict
    consumed by downstream generators.  Adds row counts, refines column types,
    and attaches hyper-specific metadata.

    Args:
        datasource: Datasource dict from ``extract_datasource()``.
        hyper_tables: list of table dicts from ``hyper_reader.read_hyper()``
            (the ``tables`` field of the reader result).

    Returns:
        datasource dict (mutated in-place for convenience).
    """
    if not hyper_tables:
        return datasource

    # Build lookup: normalised table name → hyper table info
    hyper_lookup = {}
    for ht in hyper_tables:
        raw_name = ht.get('table', '')
        # Normalise: strip schema prefix like "Extract.Extract" → "Extract"
        norm = raw_name.rsplit('.', 1)[-1].lower()
        hyper_lookup[norm] = ht
        hyper_lookup[raw_name.lower()] = ht

    for table in datasource.get('tables', []):
        tbl_name = table.get('name', '')
        norm_tbl = tbl_name.rsplit('.', 1)[-1].lower()
        ht = hyper_lookup.get(norm_tbl) or hyper_lookup.get(tbl_name.lower())
        if not ht:
            continue

        # Enrich row count
        if ht.get('row_count'):
            table['hyper_row_count'] = ht['row_count']

        # Refine column types from Hyper when XML type is missing or generic
        hyper_col_map = {
            c['name'].lower(): c for c in ht.get('columns', [])
        }
        for col in table.get('columns', []):
            hc = hyper_col_map.get(col.get('name', '').lower())
            if hc:
                existing = (col.get('datatype') or '').lower()
                hyper_type = (hc.get('hyper_type') or '').lower()
                if not existing or existing == 'string':
                    # Map common Hyper types to Tableau-style types
                    type_map = {
                        'bigint': 'integer', 'integer': 'integer',
                        'smallint': 'integer', 'int': 'integer',
                        'double': 'real', 'real': 'real', 'float': 'real',
                        'double precision': 'real', 'numeric': 'real',
                        'boolean': 'boolean', 'bool': 'boolean',
                        'date': 'date', 'timestamp': 'datetime',
                        'timestamp without time zone': 'datetime',
                        'timestamptz': 'datetime',
                    }
                    mapped = type_map.get(hyper_type)
                    if mapped:
                        col['datatype'] = mapped
                        col['hyper_type_source'] = hyper_type

        # Attach column statistics
        col_stats = ht.get('column_stats', {})
        if col_stats:
            table['hyper_column_stats'] = col_stats

    # Tag the datasource so downstream knows Hyper enrichment happened
    datasource['hyper_enriched'] = True
    datasource['hyper_table_count'] = len(hyper_tables)
    datasource['hyper_total_rows'] = sum(
        t.get('row_count', 0) for t in hyper_tables
    )

    return datasource


# ── Type coercion detection ────────────────────────────────────────────────────

# Tableau type patterns that look like auto-coerced values
_COERCION_PATTERNS = {
    ('string', 'date'): 'date',
    ('string', 'datetime'): 'datetime',
    ('string', 'integer'): 'integer',
    ('string', 'real'): 'real',
}


def detect_type_coercions(datasource):
    """Detect columns where Tableau may auto-coerce types.

    Compares the raw source type (from ``<relation>`` column metadata) against
    the semantic type (from ``<column>`` metadata).  When they differ in a way
    that indicates implicit coercion (e.g. string→date), returns a list of
    coercion hints that should be emitted as explicit ``Table.TransformColumnTypes``
    steps in Power Query M.

    Args:
        datasource: Datasource dict from ``extract_datasource()``.

    Returns:
        list of dicts ``{'table', 'column', 'from_type', 'to_type'}``
    """
    coercions = []
    col_metadata = {c['name']: c for c in datasource.get('columns', [])
                    if isinstance(c, dict)}

    for table in datasource.get('tables', []):
        tbl_name = table.get('name', '')
        for col in table.get('columns', []):
            col_name = col.get('name', '')
            raw_type = (col.get('datatype') or 'string').lower()
            # Check if metadata declares a different semantic type
            meta = col_metadata.get(col_name, {})
            semantic_type = (meta.get('datatype') or raw_type).lower()

            key = (raw_type, semantic_type)
            if key in _COERCION_PATTERNS:
                coercions.append({
                    'table': tbl_name,
                    'column': col_name,
                    'from_type': raw_type,
                    'to_type': semantic_type,
                })
    return coercions


def resolve_published_datasource(datasource, server_client=None):
    """Resolve a published (sqlproxy) datasource via Tableau Server API.

    If the datasource connection type is 'Tableau Server' (sqlproxy), and a
    ``server_client`` is provided, fetch the published datasource definition
    and merge its tables, columns, and connection info into the local datasource.

    Args:
        datasource: Datasource dict from extract_datasource().
        server_client: Optional TableauServerClient (from server_client.py).

    Returns:
        The datasource dict (mutated in place, or unchanged if not sqlproxy).
    """
    conn = datasource.get('connection', {})
    if conn.get('type') != 'Tableau Server':
        return datasource

    ds_name = conn.get('details', {}).get('server_ds_name', '')
    if not ds_name or server_client is None:
        # Mark as unresolved — caller can warn or skip
        datasource['_published_unresolved'] = True
        return datasource

    try:
        # Attempt to list datasources and find matching one
        remote_datasources = server_client.list_datasources()
        match = None
        for rds in remote_datasources:
            if rds.get('name', '') == ds_name:
                match = rds
                break
        # Accent-insensitive fallback
        if not match:
            import unicodedata as _ud
            def _norm(s):
                return ''.join(c for c in _ud.normalize('NFKD', s)
                               if not _ud.combining(c)).lower()
            norm_ds = _norm(ds_name)
            for rds in remote_datasources:
                if _norm(rds.get('name', '')) == norm_ds:
                    match = rds
                    break

        if not match:
            datasource['_published_unresolved'] = True
            logger.warning('Published datasource %r not found on server', ds_name)
            return datasource

        # Download and extract the remote datasource
        ds_id = match.get('id', '')
        if ds_id and hasattr(server_client, 'download_datasource'):
            import tempfile
            tmp_path = server_client.download_datasource(ds_id)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    remote_ds = _parse_published_datasource_file(tmp_path)
                    if remote_ds:
                        # Merge: take connection, tables, columns from remote
                        if remote_ds.get('connection'):
                            datasource['connection'] = remote_ds['connection']
                        if remote_ds.get('tables'):
                            datasource['tables'] = remote_ds['tables']
                        if remote_ds.get('columns'):
                            datasource['columns'] = remote_ds['columns']
                        if remote_ds.get('relationships'):
                            datasource['relationships'] = remote_ds['relationships']
                        if remote_ds.get('relation_metadata'):
                            datasource['relation_metadata'] = remote_ds['relation_metadata']
                        if remote_ds.get('connection_map'):
                            datasource['connection_map'] = remote_ds['connection_map']
                        datasource['_published_resolved'] = True
                finally:
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass
    except Exception as e:
        logger.warning('Failed to resolve published datasource %r: %s', ds_name, e)
        datasource['_published_unresolved'] = True

    return datasource


def _parse_published_datasource_file(file_path):
    """Parse a downloaded .tdsx/.tds file into a datasource dict."""
    try:
        # Import safe_parse_xml for XXE protection on untrusted files
        try:
            from powerbi_import.security_validator import safe_parse_xml
        except ImportError:
            safe_parse_xml = None

        ext = os.path.splitext(file_path)[1].lower()
        if ext == '.tdsx':
            with zipfile.ZipFile(file_path, 'r') as z:
                tds_names = [n for n in z.namelist() if n.endswith('.tds')]
                if not tds_names:
                    return None
                with z.open(tds_names[0]) as f:
                    xml_content = f.read()
        else:
            with open(file_path, 'rb') as f:
                xml_content = f.read()

        if safe_parse_xml is not None:
            root = safe_parse_xml(xml_content)
        else:
            root = ET.fromstring(xml_content)
        ds_elem = root.find('.//datasource')
        if ds_elem is None:
            ds_elem = root if root.tag == 'datasource' else None
        if ds_elem is None:
            return None
        return extract_datasource(ds_elem, twbx_path=file_path)
    except Exception as e:
        logger.warning('Could not parse published datasource file %s: %s', file_path, e)
        return None


# ═══════════════════════════════════════════════════════════════════════
#  Sprint 167 — Published Datasource Caching & Bulk Resolution
# ═══════════════════════════════════════════════════════════════════════

import json as _json
import hashlib as _hashlib


def _ds_cache_key(ds_name):
    """Produce a filesystem-safe cache key for a datasource name."""
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', ds_name)
    digest = _hashlib.sha256(ds_name.encode()).hexdigest()[:12]
    return f"{safe}_{digest}"


def cache_published_datasource(datasource, cache_dir):
    """Cache a resolved published datasource as JSON.

    Args:
        datasource: Resolved datasource dict.
        cache_dir: Directory for the cache.

    Returns:
        str: Path to the cached file.
    """
    ds_name = datasource.get('name', 'unknown')
    key = _ds_cache_key(ds_name)
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, f'{key}.json')

    cache_entry = {
        'name': ds_name,
        'connection': datasource.get('connection'),
        'tables': datasource.get('tables', []),
        'columns': datasource.get('columns', []),
        'relationships': datasource.get('relationships', []),
        'relation_metadata': datasource.get('relation_metadata', {}),
        'connection_map': datasource.get('connection_map'),
    }

    with open(path, 'w', encoding='utf-8') as f:
        _json.dump(cache_entry, f, indent=2, ensure_ascii=False, default=str)

    logger.debug("Cached published datasource %r → %s", ds_name, path)
    return path


def load_cached_datasource(ds_name, cache_dir):
    """Load a published datasource from the cache.

    Args:
        ds_name: Datasource name.
        cache_dir: Directory for the cache.

    Returns:
        dict or None: Cached datasource dict, or None if not found.
    """
    key = _ds_cache_key(ds_name)
    path = os.path.join(cache_dir, f'{key}.json')
    if not os.path.exists(path):
        return None

    try:
        with open(path, 'r', encoding='utf-8') as f:
            return _json.load(f)
    except (OSError, _json.JSONDecodeError) as e:
        logger.warning("Failed to load cached datasource %r: %s", ds_name, e)
        return None


def clear_ds_cache(cache_dir):
    """Clear the published datasource cache.

    Args:
        cache_dir: Cache directory to clear.

    Returns:
        int: Number of files removed.
    """
    if not os.path.isdir(cache_dir):
        return 0

    count = 0
    for fname in os.listdir(cache_dir):
        if fname.endswith('.json'):
            try:
                os.remove(os.path.join(cache_dir, fname))
                count += 1
            except OSError:
                pass
    logger.info("Cleared %d cached datasource(s) from %s", count, cache_dir)
    return count


def resolve_published_datasource_cached(datasource, server_client=None,
                                         cache_dir=None, no_cache=False):
    """Resolve a published datasource with optional caching.

    Checks the cache first. If not cached, resolves via server
    and caches the result. Falls back to cache if server is unavailable.

    Args:
        datasource: Datasource dict from extract_datasource().
        server_client: Optional TableauServerClient.
        cache_dir: Cache directory path. None disables caching.
        no_cache: If True, skip cache reads (still writes).

    Returns:
        The datasource dict (mutated in place).
    """
    conn = datasource.get('connection', {})
    if conn.get('type') != 'Tableau Server':
        return datasource

    ds_name = conn.get('details', {}).get('server_ds_name', '')
    if not ds_name:
        datasource['_published_unresolved'] = True
        return datasource

    # Try cache first (unless no_cache)
    if cache_dir and not no_cache:
        cached = load_cached_datasource(ds_name, cache_dir)
        if cached:
            for key in ('connection', 'tables', 'columns', 'relationships', 'relation_metadata', 'connection_map'):
                if key in cached and cached[key]:
                    datasource[key] = cached[key]
            datasource['_published_resolved'] = True
            datasource['_published_source'] = 'cache'
            logger.info("Resolved published datasource %r from cache", ds_name)
            return datasource

    # Try server
    if server_client:
        resolve_published_datasource(datasource, server_client)

        # Cache on success
        if datasource.get('_published_resolved') and cache_dir:
            cache_published_datasource(datasource, cache_dir)
            datasource['_published_source'] = 'server'
            return datasource

    # Offline fallback — try cache even if no_cache was set
    if cache_dir:
        cached = load_cached_datasource(ds_name, cache_dir)
        if cached:
            for key in ('connection', 'tables', 'columns', 'relationships', 'relation_metadata', 'connection_map'):
                if key in cached and cached[key]:
                    datasource[key] = cached[key]
            datasource['_published_resolved'] = True
            datasource['_published_source'] = 'cache_fallback'
            logger.warning("Resolved %r from stale cache (server unavailable)", ds_name)
            return datasource

    datasource['_published_unresolved'] = True
    return datasource


def resolve_all_published(datasources, server_client=None,
                           cache_dir=None, no_cache=False):
    """Bulk-resolve all published datasources in a list.

    Args:
        datasources: List of datasource dicts.
        server_client: Optional TableauServerClient.
        cache_dir: Cache directory.
        no_cache: Skip cache reads.

    Returns:
        dict: {resolved: [names], unresolved: [names], cached: [names]}
    """
    resolved, unresolved, cached = [], [], []

    for ds in datasources:
        conn = ds.get('connection', {})
        if conn.get('type') != 'Tableau Server':
            continue

        ds_name = conn.get('details', {}).get('server_ds_name', ds.get('name', ''))
        resolve_published_datasource_cached(
            ds, server_client=server_client,
            cache_dir=cache_dir, no_cache=no_cache)

        if ds.get('_published_resolved'):
            source = ds.get('_published_source', 'server')
            if source in ('cache', 'cache_fallback'):
                cached.append(ds_name)
            else:
                resolved.append(ds_name)
        else:
            unresolved.append(ds_name)

    logger.info("Published DS resolution: %d resolved, %d cached, %d unresolved",
                len(resolved), len(cached), len(unresolved))
    return {
        'resolved': resolved,
        'unresolved': unresolved,
        'cached': cached,
    }


def _with_native_source_context(connection, inner_conn, source_path):
    """Attach loose-source provenance without opening any native dataset."""
    details = connection['details']
    # This private marker comes only from caller-owned source context, not XML.
    details.pop('_source_base_dir', None)
    if (not source_path
            or inner_conn.get('class', '').lower() not in {
                'csv', 'textscan', 'excel', 'excel-direct', 'geojson',
                'ogrdirect', 'json', 'xml', 'pdf', 'parquet'}
            or os.path.splitext(source_path)[1].lower() not in {
                '.twb', '.twbx', '.tds', '.tdsx'}
            or not os.path.isfile(source_path)):
        return connection

    try:
        # Inspect only the original workbook/datasource container. In
        # particular, XML misnamed .twbx is loose; real ZIP resources are not.
        with open(source_path, 'rb') as workbook:
            if zipfile.is_zipfile(workbook):
                return connection
    except OSError as exc:
        logger.warning(
            "Could not inspect native source workbook %r: %s; relative file "
            "bindings require explicit configuration.", source_path, exc)
        return connection

    details['_source_base_dir'] = os.path.dirname(os.path.abspath(source_path))
    # Excel's legacy mapping omits directory; retain it verbatim when present.
    if 'directory' in inner_conn.attrib:
        details.setdefault('directory', inner_conn.get('directory', ''))
    return connection


def _parse_connection_class(inner_conn, named_conn=None, twbx_path=None):
    """Parses a single Tableau <connection> element into {type, details}.
    
    This is the single source of truth for mapping Tableau connection XML
    attributes to the normalized {type, details} dicts used downstream.
    
    Args:
        inner_conn: XML <connection> element
        named_conn: Optional XML <named-connection> parent (for caption/name)
        twbx_path: Optional original source path (archive delimiter detection
            or loose native-file source provenance)
    
    Returns:
        dict: {type: str, details: dict}
    """
    conn_class = inner_conn.get('class', 'unknown')

    # ── Dispatch table for simple attribute-mapping connectors ──────────────
    # Each entry: conn_class → (type_name, {detail_key: xml_attr_or_default, ...})
    _SIMPLE_CONNECTORS = {
        'excel-direct': ('Excel', {
            'filename': ('filename', ''),
            'cleaning': ('cleaning', 'no'),
            'compat': ('compat', 'no'),
        }),
        'ogrdirect': ('GeoJSON', {
            'filename': ('filename', ''),
            'directory': ('directory', ''),
        }),
        'sqlserver': ('SQL Server', {
            'server': ('server', ''),
            'database': ('dbname', ''),
            'authentication': ('authentication', 'sspi'),
            'username': ('username', ''),
        }),
        'postgres': ('PostgreSQL', {
            'server': ('server', ''),
            'port': ('port', '5432'),
            'database': ('dbname', ''),
            'username': ('username', ''),
            'sslmode': ('sslmode', 'require'),
        }),
        'bigquery': ('BigQuery', {
            'project': ('project', ''),
            'dataset': ('dataset', ''),
            'service_account': ('service-account-email', ''),
        }),
        'oracle': ('Oracle', {
            'server': ('server', ''),
            'service': ('service', ''),
            'port': ('port', '1521'),
            'username': ('username', ''),
        }),
        'mysql': ('MySQL', {
            'server': ('server', ''),
            'port': ('port', '3306'),
            'database': ('dbname', ''),
            'username': ('username', ''),
        }),
        'snowflake': ('Snowflake', {
            'server': ('server', ''),
            'database': ('dbname', ''),
            'schema': ('schema', ''),
            'warehouse': ('warehouse', ''),
            'role': ('role', ''),
        }),
        'sapbw': ('SAP BW', {
            'server': ('server', ''),
            'system_number': ('systemNumber', '00'),
            'client_id': ('clientId', ''),
            'language': ('language', 'EN'),
            'cube': ('cube', ''),
            'catalog': ('catalog', ''),
        }),
        # Sprint 181: enterprise connector auto-detection
        'dremio': ('Dremio', {
            'server': ('server', ''),
            'port': ('port', '31010'),
            'schema': ('schema', ''),
            'username': ('username', ''),
        }),
        'clickhouse': ('ClickHouse', {
            'server': ('server', ''),
            'port': ('port', '8123'),
            'database': ('dbname', 'default'),
            'username': ('username', ''),
        }),
        'singlestore': ('SingleStore', {
            'server': ('server', ''),
            'port': ('port', '3306'),
            'database': ('dbname', ''),
            'username': ('username', ''),
        }),
        'memsql': ('SingleStore', {
            'server': ('server', ''),
            'port': ('port', '3306'),
            'database': ('dbname', ''),
            'username': ('username', ''),
        }),
        'firebolt': ('Firebolt', {
            'server': ('server', ''),
            'database': ('dbname', ''),
            'username': ('username', ''),
        }),
        'starburst': ('Starburst', {
            'server': ('server', ''),
            'port': ('port', '443'),
            'catalog': ('catalog', ''),
            'schema': ('schema', 'default'),
            'username': ('username', ''),
        }),
        'trino': ('Starburst', {
            'server': ('server', ''),
            'port': ('port', '443'),
            'catalog': ('catalog', ''),
            'schema': ('schema', 'default'),
            'username': ('username', ''),
        }),
        'db2': ('IBM Db2 Deep', {
            'server': ('server', ''),
            'port': ('port', '50000'),
            'database': ('dbname', ''),
            'schema': ('schema', ''),
            'username': ('username', ''),
        }),
        'teradata': ('Teradata Deep', {
            'server': ('server', ''),
            'database': ('dbname', ''),
            'username': ('username', ''),
        }),
        'vertica': ('Vertica', {
            'server': ('server', ''),
            'port': ('port', '5433'),
            'database': ('dbname', ''),
            'username': ('username', ''),
        }),
    }

    # ── Special cases (need extra logic) ────────────────────────────────────
    if conn_class == 'excel-direct':
        result = _build_from_dispatch(inner_conn, _SIMPLE_CONNECTORS['excel-direct'])
        result['details']['caption'] = named_conn.get('caption', '') if named_conn is not None else ''
        return _with_native_source_context(result, inner_conn, twbx_path)

    if conn_class == 'textscan':
        csv_filename = inner_conn.get('filename', '')
        csv_directory = inner_conn.get('directory', '')
        delimiter = inner_conn.get('separator', '')
        if not delimiter:
            header = _read_csv_header_from_twbx(twbx_path, csv_directory, csv_filename)
            delimiter = _detect_csv_delimiter(header) if header else ','
        return _with_native_source_context({
            'type': 'CSV',
            'details': {
                'filename': csv_filename,
                'directory': csv_directory,
                'delimiter': delimiter,
                'encoding': inner_conn.get('charset', 'utf-8')
            }
        }, inner_conn, twbx_path)

    # ── Dispatch simple connectors ──────────────────────────────────────────
    if conn_class in _SIMPLE_CONNECTORS:
        return _with_native_source_context(
            _build_from_dispatch(inner_conn, _SIMPLE_CONNECTORS[conn_class]),
            inner_conn, twbx_path)

    # ── sqlproxy: Tableau Server Published Datasource ──────────────────────
    if conn_class == 'sqlproxy':
        return {
            'type': 'Tableau Server',
            'details': {
                'server': inner_conn.get('server', ''),
                'port': inner_conn.get('port', '443'),
                'dbname': inner_conn.get('dbname', ''),
                'channel': inner_conn.get('channel', 'https'),
                'server_ds_name': inner_conn.get('server-ds-friendly-name', ''),
            }
        }

    # ── Fallback for unknown connector types ────────────────────────────────
    return _with_native_source_context({
        'type': conn_class.upper(),
        'details': dict(inner_conn.attrib)
    }, inner_conn, twbx_path)


def _build_from_dispatch(inner_conn, spec):
    """Build a {type, details} dict from a dispatch table spec."""
    type_name, attr_map = spec
    details = {}
    for detail_key, (xml_attr, default) in attr_map.items():
        details[detail_key] = inner_conn.get(xml_attr, default)
    return {'type': type_name, 'details': details}


def _build_connection_map(datasource_elem, twbx_path=None):
    """Builds a connection_name -> {type, details} mapping from named-connections.
    
    Each physical table in Tableau references a named-connection via its
    'connection' attribute. This function extracts the details of each named-connection
    to generate the correct M queries per table.
    """
    conn_map = {}
    
    connection_elem = datasource_elem.find('.//connection[@class="federated"]')
    if connection_elem is None:
        connection_elem = datasource_elem.find('.//connection')
    if connection_elem is None:
        return conn_map
    
    for named_conn in connection_elem.findall('.//named-connection'):
        nc_name = named_conn.get('name', '')
        inner_conn = named_conn.find('.//connection')
        if inner_conn is None or not nc_name:
            continue
        conn_map[nc_name] = _parse_connection_class(inner_conn, named_conn, twbx_path)
    
    return conn_map


def extract_connection_details(datasource_elem, twbx_path=None):
    """Extracts connection details (Excel, SQL, etc.)"""
    connection_elem = datasource_elem.find('.//connection[@class="federated"]')
    if connection_elem is None:
        connection_elem = datasource_elem.find('.//connection')
    if connection_elem is None:
        return {'type': 'Unknown', 'details': {}}
    
    named_conn = connection_elem.find('.//named-connection')
    if named_conn is not None:
        inner_conn = named_conn.find('.//connection')
        if inner_conn is not None:
            # Add provenance separately: do not introduce packaged CSV reads
            # into this previously metadata-only connection path.
            return _with_native_source_context(
                _parse_connection_class(inner_conn, named_conn),
                inner_conn, twbx_path)
    
    # Direct connection (no named-connection wrapper) — e.g. sqlproxy
    conn_class = connection_elem.get('class', '')
    if conn_class and conn_class != 'federated':
        return _with_native_source_context(
            _parse_connection_class(connection_elem), connection_elem, twbx_path)

    return {'type': 'Unknown', 'details': {}}


def extract_tables_with_columns(datasource_elem, connection_map=None):
    """Extracts only physical tables (type='table') with their columns.
    
    IMPORTANT: Do NOT extract 'join' nodes which created fictitious tables
    with duplicated columns from all joined tables ('Unknown' table bug).
    
    Deduplicates by table name (keeps the version with the most columns)
    and stores per-table connection details.
    
    For SQL Server and similar connections where <relation> elements are
    self-closing (no nested <columns>), falls back to the datasource-level
    <cols> mapping and <column> definitions to populate table columns.
    """
    if connection_map is None:
        connection_map = {}
    datasource_elem, _ = _native_datasource_context(datasource_elem)
    
    # Phase 1: Collect all physical tables
    raw_tables = {}  # name -> best table dict
    
    for relation in datasource_elem.findall('.//relation'):
        # ONLY physical tables, NOT joins
        table_type = relation.get('type', '')
        if table_type != 'table':
            continue
        
        table_name = relation.get('name', '')
        if not table_name:
            continue
        
        conn_ref = relation.get('connection', '')
        
        columns = []
        for col_elem in relation.findall('./columns/column'):
            raw_name = col_elem.get('name', '')
            datatype = col_elem.get('datatype', 'string')
            column = {
                'name': _reverse_tableau_bracket_escape(raw_name),
                'datatype': datatype,
                'role': 'measure' if datatype in ('real', 'integer') else 'dimension',
                'ordinal': int(col_elem.get('ordinal', 0)),
                'length': col_elem.get('length', None),
                'nullable': col_elem.get('nullable', 'true') == 'true',
                'default_format': col_elem.get('default-format', ''),
            }
            columns.append(column)
        
        # Deduplicate: keep the version with the most columns
        if table_name not in raw_tables or len(columns) > len(raw_tables[table_name].get('columns', [])):
            # Resolve connection details for this table
            table_connection = connection_map.get(conn_ref, {})
            
            raw_tables[table_name] = {
                'name': table_name,
                'type': 'table',
                'columns': columns,
                'connection': conn_ref,
                'connection_details': table_connection,
                'caption': relation.get('caption', ''),
                'source_table': relation.get('table', '').strip('[]'),
            }
    
    # Phase 2: For tables with no nested columns (SQL Server, etc.),
    # populate from datasource-level <cols> mapping + <column> elements.
    tables_needing_columns = [t for t in raw_tables.values() if not t['columns']]
    if tables_needing_columns:
        # Build mapping: table_name -> [column_name, ...] from <cols><map> entries
        # e.g. <map key='[OrderID]' value='[Orders].[OrderID]' />
        table_col_names = {}  # table_name -> [(col_key, remote_name), ...]
        cols_elem = datasource_elem.find('.//connection/cols')
        if cols_elem is not None:
            for map_elem in cols_elem.findall('map'):
                key = map_elem.get('key', '')       # e.g. "[OrderID]"
                value = map_elem.get('value', '')    # e.g. "[Orders].[OrderID]"
                if '.' in value:
                    parts = value.split('.', 1)
                    tbl = parts[0].strip('[]')
                    remote = parts[1].strip('[]')
                    if tbl in raw_tables:
                        table_col_names.setdefault(tbl, []).append((key, remote))
        
        # Build mapping: column_name -> column attributes from datasource-level <column> elements
        ds_columns = {}  # "[ColName]" -> {datatype, role, type, ...}
        for col_elem in datasource_elem.findall('./column'):
            col_name = col_elem.get('name', '')
            # Skip calculated columns (they have a <calculation> child)
            if col_elem.find('.//calculation') is not None:
                continue
            # Skip user-filter columns
            if col_elem.get('user:auto-column', '') == 'sheet_link':
                continue
            datatype = col_elem.get('datatype', 'string')
            # Infer role: explicit role attribute takes precedence,
            # otherwise numeric types default to 'measure' (Tableau convention)
            explicit_role = col_elem.get('role', '')
            if explicit_role:
                role = explicit_role
            elif datatype in ('real', 'integer'):
                role = 'measure'
            else:
                role = 'dimension'
            ds_columns[col_name] = {
                'name': col_name.strip('[]'),
                'datatype': datatype,
                'role': role,
                'ordinal': 0,
                'length': None,
                'nullable': True,
                'default_format': col_elem.get('default-format', ''),
            }
        
        # Build metadata-record type lookup for columns missing from
        # datasource-level <column> elements (e.g. Salesforce Probability).
        metadata_type_map = {}  # "[ColName]" -> local-type
        for mr in datasource_elem.findall('.//metadata-record[@class="column"]'):
            local_name = (mr.findtext('local-name') or '').strip()
            local_type = (mr.findtext('local-type') or '').strip()
            if local_name and local_type:
                metadata_type_map[local_name] = local_type

        # Populate columns for each table that needs them
        for table in tables_needing_columns:
            tname = table['name']
            col_keys = table_col_names.get(tname, [])
            ordinal = 0
            for key, remote in col_keys:
                if key in ds_columns:
                    col = dict(ds_columns[key])
                    col['ordinal'] = ordinal
                    col['remote_name'] = remote
                    ordinal += 1
                    table['columns'].append(col)
                elif key in metadata_type_map:
                    # Column exists in <cols>/<map> and metadata-records
                    # but has no <column> element (e.g. Salesforce fields
                    # like Probability that are physical but not declared).
                    dtype = metadata_type_map[key]
                    col = {
                        'name': key.strip('[]'),
                        'datatype': dtype,
                        'role': 'measure' if dtype in ('real', 'integer') else 'dimension',
                        'ordinal': ordinal,
                        'length': None,
                        'nullable': True,
                        'remote_name': remote,
                    }
                    ordinal += 1
                    table['columns'].append(col)
    
    # Phase 2.5: Override column roles from datasource-level <column> elements.
    # Users may override Tableau's default role (e.g. set an integer column
    # like "Row ID" to dimension). These overrides are stored at the
    # datasource level as explicit role attributes.
    ds_role_overrides = {}
    for col_elem in datasource_elem.findall('./column'):
        col_name = col_elem.get('name', '').strip('[]')
        explicit_role = col_elem.get('role', '')
        if col_name and explicit_role:
            ds_role_overrides[col_name] = explicit_role
    if ds_role_overrides:
        for table in raw_tables.values():
            for col in table.get('columns', []):
                cname = col.get('name', '')
                if cname in ds_role_overrides:
                    col['role'] = ds_role_overrides[cname]

    # Phase 3: Merge missing metadata-record columns into physical tables.
    # Unmatched parent aliases are safe only when the datasource has one table.
    if raw_tables:
        column_declarations = {
            _reverse_tableau_bracket_escape(col_elem.get('name', '').strip('[]')): col_elem
            for col_elem in datasource_elem.findall('./column')
            if col_elem.find('.//calculation') is None
            and col_elem.get('user:auto-column', '') != 'sheet_link'
        }
        singleton_name = next(iter(raw_tables)) if len(raw_tables) == 1 else None
        metadata_table_cols = {}
        for mr in datasource_elem.findall('.//metadata-record[@class="column"]'):
            remote_name = (mr.findtext('remote-name') or '').strip()
            local_name = (mr.findtext('local-name') or '').strip()
            parent_name = (mr.findtext('parent-name') or '').strip().strip('[]')
            local_type = (mr.findtext('local-type') or 'string').strip()
            ordinal_text = (mr.findtext('ordinal') or '0').strip()
            contains_null = (mr.findtext('contains-null') or 'true').strip()

            col_name = _reverse_tableau_bracket_escape(local_name.strip('[]') if local_name else remote_name)
            if not col_name:
                continue
            if parent_name not in raw_tables:
                if singleton_name is None:
                    continue
                parent_name = singleton_name

            declaration = column_declarations.get(col_name)
            if declaration is not None:
                local_type = declaration.get('datatype', local_type)

            try:
                ordinal_val = int(ordinal_text)
            except ValueError:
                ordinal_val = 0

            col = {
                'name': col_name,
                'datatype': local_type,
                'role': 'measure' if local_type in ('real', 'integer') else 'dimension',
                'ordinal': ordinal_val,
                'length': None,
                'nullable': contains_null == 'true',
            }
            if declaration is not None:
                col['role'] = declaration.get('role') or col['role']
                col['default_format'] = declaration.get('default-format', '')
            metadata_table_cols.setdefault(parent_name, []).append(col)

        for table in raw_tables.values():
            tname = table['name']
            meta_cols = metadata_table_cols.get(tname, [])
            existing_names = {col['name'] for col in table['columns']}
            for col in sorted(meta_cols, key=lambda c: c['ordinal']):
                if col['name'] not in existing_names:
                    table['columns'].append(col)
                    existing_names.add(col['name'])

    # Phase 4: Merge missing datasource-level physical declarations into the
    # sole table, including partially populated schemas. With multiple tables,
    # declarations without ownership evidence must not be assigned.
    if len(raw_tables) == 1:
        table = next(iter(raw_tables.values()))
        existing_names = {col['name'] for col in table['columns']}
        ordinal = len(table['columns'])
        for col_name, col_elem in column_declarations.items():
            if not col_name or col_name.startswith(':') or col_name in existing_names:
                continue
            datatype = col_elem.get('datatype', 'string')
            table['columns'].append({
                'name': col_name,
                'datatype': datatype,
                'role': col_elem.get('role') or (
                    'measure' if datatype in ('real', 'integer') else 'dimension'),
                'ordinal': ordinal,
                'length': None,
                'nullable': True,
            })
            existing_names.add(col_name)
            ordinal += 1

    # Filter out 0-column tables when other tables have columns
    # (e.g. Tableau extract artifacts like 'Extract' tables in .twbx files)
    tables = list(raw_tables.values())
    has_populated = any(t['columns'] for t in tables)
    if has_populated:
        tables = [t for t in tables if t['columns']]

    # Build per-table physical→display column name mapping from <cols><map>.
    # This captures ALL mappings regardless of which phase added the column,
    # enabling CSV Hyper data rewrite to rename physical column headers.
    cols_elem = datasource_elem.find('.//connection/cols')
    if cols_elem is not None:
        physical_maps = {}  # table_name → {physical_name: display_name}
        for map_elem in cols_elem.findall('map'):
            key = map_elem.get('key', '').strip('[]')
            value = map_elem.get('value', '')
            if '.' in value:
                parts = value.split('.', 1)
                tbl = parts[0].strip('[]')
                remote = parts[1].strip('[]')
                if key and remote and key != remote:
                    physical_maps.setdefault(tbl, {})[remote] = key
        for t in tables:
            pm = physical_maps.get(t['name'])
            if pm:
                t['cols_physical_map'] = pm

    return tables


def extract_table_extensions(datasource_elem):
    """Extracts Tableau 2024.2+ table extensions (Einstein Discovery, external API data).

    Table extensions allow Tableau to augment datasources with data from
    external APIs or Einstein Discovery predictions. This function extracts
    their configuration for migration to Power Query Web.Contents() or
    placeholder notes.

    Returns:
        list of dicts with extension_type, name, endpoint, schema, config.
    """
    extensions = []
    for ext_elem in datasource_elem.findall('.//table-extension'):
        ext_type = ext_elem.get('type', 'unknown')
        ext_name = ext_elem.get('name', ext_elem.get('caption', ''))

        # Extract API endpoint if present
        endpoint = ''
        config_elem = ext_elem.find('.//connection')
        if config_elem is not None:
            endpoint = config_elem.get('url', config_elem.get('server', ''))

        # Extract schema (output columns)
        schema = []
        for col_elem in ext_elem.findall('.//column'):
            schema.append({
                'name': col_elem.get('name', '').strip('[]'),
                'datatype': col_elem.get('datatype', 'string'),
            })

        # Extract extension configuration
        config = {}
        for attr in ext_elem.attrib:
            if attr not in ('type', 'name', 'caption'):
                config[attr] = ext_elem.get(attr)

        # Check for nested config elements
        for cfg_elem in ext_elem.findall('.//configuration/*'):
            config[cfg_elem.tag] = cfg_elem.text or cfg_elem.get('value', '')

        extensions.append({
            'name': ext_name or f'Extension_{len(extensions) + 1}',
            'extension_type': ext_type,
            'endpoint': endpoint,
            'schema': schema,
            'config': config,
        })

    return extensions


def extract_column_metadata(datasource_elem):
    """Extracts complete column metadata"""
    columns = []
    
    for col_elem in datasource_elem.findall('.//column'):
        column = {
            'name': col_elem.get('name', ''),
            'caption': col_elem.get('caption', ''),
            'datatype': col_elem.get('datatype', 'string'),
            'role': col_elem.get('role', 'dimension'),
            'type': col_elem.get('type', 'nominal'),
            'hidden': col_elem.get('hidden', 'false') == 'true',
            'semantic_role': col_elem.get('semantic-role', ''),
            'default_aggregation': col_elem.get('default-type', ''),
            'default_format': col_elem.get('default-format', ''),
            'description': col_elem.get('desc', ''),
            'calculation': None
        }
        
        # Check if it is a calculation
        calc_elem = col_elem.find('.//calculation')
        if calc_elem is not None:
            column['calculation'] = {
                'class': calc_elem.get('class', 'tableau'),
                'formula': _extract_calculation_formula(
                    calc_elem, col_elem.get('name', ''))
            }
        
        columns.append(column)
    
    return columns


def extract_calculations(datasource_elem):
    """Extracts Tableau calculations with formulas.
    
    Also extracts <table-calc> elements for COMPUTE USING (addressing)
    so that DAX generation can use proper filter context.
    """
    calculations = []
    
    for col_elem in datasource_elem.findall('.//column'):
        calc_elem = col_elem.find('.//calculation')
        if calc_elem is not None:
            calc_class = calc_elem.get('class', 'tableau')
            # Skip categorical-bin calculations — they are handled by group
            # extraction and have no formula, which would produce empty measures.
            if calc_class == 'categorical-bin':
                continue
            calc_formula = _extract_calculation_formula(
                calc_elem, col_elem.get('name', ''))
            # Skip calculations with no formula to avoid empty measures
            if not calc_formula.strip():
                continue
            calculation = {
                'name': col_elem.get('name', ''),
                'caption': col_elem.get('caption', col_elem.get('name', '')),
                'formula': calc_formula,
                'class': calc_class,
                'datatype': col_elem.get('datatype', 'real'),
                'role': col_elem.get('role', 'measure'),
                'type': col_elem.get('type', 'quantitative'),
                'description': col_elem.get('desc', '')
            }
            
            # Extract table-calc addressing (COMPUTE USING dimensions)
            table_calc = calc_elem.find('.//table-calc')
            if table_calc is not None:
                addressing_fields = []
                for addr in table_calc.findall('.//addressing-field'):
                    field_name = addr.get('name', addr.text or '')
                    if field_name:
                        # Clean [datasource].[field] format
                        match = re.search(r'\[([^\]]+)\]$', field_name)
                        addressing_fields.append(match.group(1) if match else field_name)
                
                partition_fields = []
                for part in table_calc.findall('.//partitioning-field'):
                    field_name = part.get('name', part.text or '')
                    if field_name:
                        match = re.search(r'\[([^\]]+)\]$', field_name)
                        partition_fields.append(match.group(1) if match else field_name)
                
                if addressing_fields or partition_fields:
                    calculation['table_calc_addressing'] = addressing_fields
                    calculation['table_calc_partitioning'] = partition_fields
                    calculation['table_calc_type'] = table_calc.get('type', '')
                    calculation['table_calc_ordering'] = table_calc.get('ordering-type', '')
            
            calculations.append(calculation)
    
    return calculations


def _relation_xml_name(name):
    """Unwrap namespaces and Tableau feature-versioned tag/attribute names."""
    return name.rsplit('}', 1)[-1].rsplit('...', 1)[-1]


def _relation_children(elem, tag):
    """Select the active (true) feature branch, never its legacy false copy."""
    return [child for child in elem if _relation_xml_name(child.tag) == tag
            and '.false...' not in child.tag]


def _relation_attr(elem, name, default=''):
    if elem is None:
        return default
    active = [value for key, value in elem.attrib.items()
              if _relation_xml_name(key) == name and '.true...' in key]
    if active:
        return active[0]
    return elem.get(name, default)


def _relation_ref_parts(reference):
    """Parse only complete field references, including escaped closing brackets."""
    parts = re.findall(r'\[((?:[^\]]|\]\])*)\]', reference)
    if not parts or re.sub(r'\[((?:[^\]]|\]\])*)\]', '', reference).strip(' .'):
        return []
    return [_reverse_tableau_bracket_escape(part) for part in parts]


def extract_relation_metadata(datasource_elem):
    """Return version-1 physical/logical join provenance, not model edges.

    ``physical_relations`` are ordered trees (type/name/connection/source_table,
    children, and for joins join_type/predicate). ``logical_tables`` bind object
    ids and display captions to those trees. ``logical_relationships`` retain
    endpoint ids/table aliases and the complete predicate.

    Predicates use {operator, left, right} for comparisons and
    {operator, arguments} for boolean/function expressions. Field operands carry
    table (relation alias), column (local field identity), remote_column
    (connector column), and reference (original XML op). Unresolved expressions
    are preserved, never coerced to equality. Active feature-versioned attrs
    take precedence over compatibility attrs.

    requires_materialization is true unless the complete predicate is a single
    resolved equality. No join cardinality, uniqueness or output grain is
    inferred from keys or shared source filenames.
    """
    result = {'version': 1, 'physical_relations': [], 'logical_tables': [],
              'logical_relationships': []}
    if datasource_elem is None:
        return result
    # This structure describes native physical provenance and logical object
    # endpoints, not the alternative materialized extract context. Object ids
    # remain endpoint identities, never additional physical table aliases.
    datasource_elem, extract_context = _native_datasource_context(datasource_elem, force=True)
    if extract_context['extract_xml'] or extract_context['object_extract_contexts_xml']:
        result['extract_context_provenance'] = extract_context

    # Local aliases and connector column names are separate identities.
    fields = {}
    for mr in datasource_elem.iter():
        if _relation_xml_name(mr.tag) != 'metadata-record' or mr.get('class') != 'column':
            continue
        values = {_relation_xml_name(child.tag): (child.text or '').strip() for child in mr}
        local = _relation_ref_parts(values.get('local-name', ''))
        parent = _relation_ref_parts(values.get('parent-name', ''))
        if local and parent:
            fields.setdefault(local[-1], []).append({
                'table': parent[-1], 'column': local[-1],
                'remote_column': values.get('remote-name') or local[-1]})
    for elem in datasource_elem.iter():
        if _relation_xml_name(elem.tag) != 'cols':
            continue
        for mapping in _relation_children(elem, 'map'):
            local = _relation_ref_parts(mapping.get('key', ''))
            remote = _relation_ref_parts(mapping.get('value', ''))
            if local and len(remote) >= 2:
                field = {'table': remote[-2], 'column': local[-1],
                         'remote_column': remote[-1]}
                candidates = fields.setdefault(local[-1], [])
                if field not in candidates:
                    candidates.append(field)

    def operand(reference, tables):
        parts = _relation_ref_parts(reference)
        if not parts:
            return {'expression': reference}
        column = parts[-1]
        table = parts[-2] if len(parts) > 1 else ''
        # Alias identity is authoritative even when a comparison reverses the
        # endpoint order. Endpoint scope only disambiguates repeated bare names.
        candidates = [
            field for values in fields.values() for field in values
            if ((field['column'] == column if not table else
                 column in (field['column'], field['remote_column']))
                and (not table or field['table'] == table))]
        candidates = [dict(items) for items in sorted({
            tuple(sorted(field.items())) for field in candidates})]
        if not table and tables:
            candidates = [field for field in candidates if field['table'] in tables]
        if len(candidates) == 1:
            field = dict(candidates[0])
        else:
            # An ambiguous nested-join side must not be assigned its first leaf.
            field = {'table': table or (tables[0] if len(tables) == 1 else ''),
                     'column': column, 'remote_column': column}
        return dict(field, reference=reference)

    comparisons = {'=', '==', '!=', '<>', '<', '<=', '>', '>='}

    def expression(elem, left_tables, right_tables):
        op = _relation_attr(elem, 'op')
        args = _relation_children(elem, 'expression')
        if op in comparisons and len(args) == 2:
            def side(arg, tables):
                if not _relation_children(arg, 'expression'):
                    return operand(_relation_attr(arg, 'op'), tables)
                return expression(arg, left_tables, right_tables)
            return {'operator': op, 'left': side(args[0], left_tables),
                    'right': side(args[1], right_tables)}
        return {'operator': op, 'arguments': [
            expression(arg, left_tables, right_tables) for arg in args]}

    def predicate(elem, left_tables, right_tables):
        expressions = _relation_children(elem, 'expression')
        if expressions:
            parsed = [expression(e, left_tables, right_tables) for e in expressions]
            return parsed[0] if len(parsed) == 1 else {'operator': 'AND', 'arguments': parsed}
        raw = _relation_attr(elem, 'expression')
        # Attribute expressions are parsed only if the entire expression is a
        # single comparison. Anything more complex remains explicitly opaque.
        ref = r'\[(?:[^\]]|\]\])*\](?:\s*\.\s*\[(?:[^\]]|\]\])*\])*'
        match = re.fullmatch(rf'\s*({ref})\s*(>=|<=|<>|!=|==|=|>|<)\s*({ref})\s*', raw)
        if match:
            return {'operator': match[2], 'left': operand(match[1], left_tables),
                    'right': operand(match[3], right_tables)}
        return {'operator': '', 'expression': raw}

    def aliases(tree):
        if tree['type'] in ('table', 'text'):
            return [tree['name']] if tree['name'] else []
        return [name for child in tree.get('children', []) for name in aliases(child)]

    def physical(elem):
        tree = {'type': _relation_attr(elem, 'type'), 'name': _relation_attr(elem, 'name'),
                'connection': _relation_attr(elem, 'connection'),
                'source_table': _relation_attr(elem, 'table')}
        children = [physical(child) for child in _relation_children(elem, 'relation')]
        if children:
            tree['children'] = children
        if tree['type'] == 'text':
            tree['sql'] = elem.text or ''
        if tree['type'] == 'join':
            tree['join_type'] = _relation_attr(elem, 'join', 'inner')
            left = aliases(children[0]) if children else []
            right = aliases(children[1]) if len(children) > 1 else []
            clauses = [predicate(clause, left, right)
                       for clause in _relation_children(elem, 'clause')]
            tree['predicate'] = (clauses[0] if len(clauses) == 1 else
                                 {'operator': 'AND', 'arguments': clauses})
            tree['requires_materialization'] = not _single_equality(tree['predicate'])
        return tree

    # Only connection-level roots: object-model copies remain on logical_tables.
    for conn in _relation_children(datasource_elem, 'connection'):
        result['physical_relations'].extend(
            physical(rel) for rel in _relation_children(conn, 'relation'))
    physical_aliases = {
        name for tree in result['physical_relations'] for name in aliases(tree)}

    for graph in datasource_elem.iter():
        if _relation_xml_name(graph.tag) != 'object-graph' or '.false...' in graph.tag:
            continue
        objects = {}
        for container in _relation_children(graph, 'objects') or [graph]:
            for obj in _relation_children(container, 'object'):
                roots = []
                # Object properties can wrap connection/relation trees.
                def collect(parent):
                    for child in parent:
                        if '.false...' in child.tag:
                            continue
                        if _relation_xml_name(child.tag) == 'relation':
                            roots.append(physical(child))
                        else:
                            collect(child)
                collect(obj)
                entry = {'object_id': _relation_attr(obj, 'id'),
                         'caption': _relation_attr(obj, 'caption'), 'relations': roots}
                objects[entry['object_id']] = entry
                result['logical_tables'].append(entry)
        containers = _relation_children(graph, 'relationships')
        for container in containers or [graph]:
            for rel in _relation_children(container, 'relationship'):
                endpoints = []
                for tag in ('first-end-point', 'second-end-point'):
                    eps = _relation_children(rel, tag)
                    obj_id = _relation_attr(eps[0], 'object-id') if eps else ''
                    obj = objects.get(obj_id, {})
                    names = [name for tree in obj.get('relations', []) for name in aliases(tree)]
                    names = list(dict.fromkeys(
                        name for name in names
                        if not physical_aliases or name in physical_aliases))
                    # Caption fallback is legacy compatibility only; never
                    # override a physical relation alias with a display label.
                    if (not names and not obj.get('relations') and obj.get('caption')
                            and (not physical_aliases or obj['caption'] in physical_aliases)):
                        names = [obj['caption']]
                    endpoints.append({'object_id': obj_id, 'tables': names})
                pred = predicate(rel, endpoints[0]['tables'], endpoints[1]['tables'])
                result['logical_relationships'].append({
                    'left': endpoints[0], 'right': endpoints[1],
                    'join_type': _relation_attr(rel, 'type').lower() or None,
                    'predicate': pred,
                    'requires_materialization': not _single_equality(pred)})
    return result


def _equality_pairs(predicate):
    """Return conjunctive equality leaves, or None for any unsupported predicate."""
    op = predicate.get('operator', '')
    if op.upper() == 'AND':
        pairs = []
        for arg in predicate.get('arguments', []):
            child_pairs = _equality_pairs(arg)
            if child_pairs is None:
                return None
            pairs.extend(child_pairs)
        return pairs or None
    if op not in ('=', '=='):
        return None
    sides = [predicate.get(side, {}) for side in ('left', 'right')]
    if not all(side.get('table') and side.get('column') for side in sides):
        return None
    return [sides]


def _single_equality(predicate):
    pairs = _equality_pairs(predicate)
    return pairs is not None and len(pairs) == 1


def extract_relationships(datasource_elem, relation_metadata=None):
    """Legacy equality-only edges. Never project part of a non-equi/OR join.

    The complete source semantics live in ``relation_metadata``. Conjunctive
    equalities retain the historical per-column shape for existing consumers;
    they do not establish uniqueness or a composite Power BI relationship.
    """
    metadata = (relation_metadata if relation_metadata is not None
                else extract_relation_metadata(datasource_elem))
    records = list(metadata['logical_relationships'])

    def joins(tree):
        if tree['type'] == 'join':
            records.append(tree)
        for child in tree.get('children', []):
            joins(child)

    for tree in metadata['physical_relations']:
        joins(tree)
    for obj in metadata['logical_tables']:
        for tree in obj['relations']:
            joins(tree)
    relationships, seen = [], set()
    for record in records:
        for left, right in _equality_pairs(record.get('predicate', {})) or []:
            key = (left['table'], left['column'], right['table'], right['column'])
            if key in seen:
                continue
            seen.add(key)
            relationships.append({
                'type': record.get('join_type') or 'left',
                'left': {'table': left['table'], 'column': left['column']},
                'right': {'table': right['table'], 'column': right['column']}})
    return relationships


# â”€â”€ Re-exports from extracted modules â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# These functions were extracted for maintainability but are re-exported here
# so that ALL existing imports remain valid (backward compatibility).

from dax_converter import (              # noqa: E402
    convert_tableau_formula_to_dax,
    map_tableau_to_powerbi_type,
    sanitize_param_brackets,
)

from m_query_builder import (            # noqa: E402
    generate_power_query_m,
    map_tableau_to_m_type,
    inject_m_steps,
    m_transform_rename,
    m_transform_remove_columns,
    m_transform_select_columns,
    m_transform_filter_values,
    m_transform_filter_nulls,
    m_transform_aggregate,
    m_transform_unpivot,
    m_transform_unpivot_other,
    m_transform_pivot,
    m_transform_join,
    m_transform_union,
    m_transform_sort,
    m_transform_add_column,
    m_transform_conditional_column,
)
