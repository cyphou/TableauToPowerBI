"""Source routing helpers for Fabric-native ingestion artifacts."""

from __future__ import annotations

import logging
from xml.etree import ElementTree
from xml.parsers import expat

from .errors import PartialGenerationError
from .security_validator import MAX_XML_SIZE_MB, SecurityError, safe_parse_xml

logger = logging.getLogger(__name__)

FILE_SOURCE_TYPES = {'csv', 'excel'}
_HYPER_SOURCE_TYPES = {'hyper', 'extract', 'dataengine'}

HYPER_SOURCE_SELECTION_UNVERIFIED = (
    "Fabric Hyper source selection is unverified: a declared Hyper member "
    "has no retained evidence establishing whether it was selected or "
    "historical. The original source may be ingested instead of the snapshot. "
    "Confirm the Tableau source choice before using this bundle. If the "
    "snapshot was selected, current Fabric ingestion cannot establish its "
    "complete export and verified column binding; do not rely on this bundle."
)


def table_connection(datasource, table):
    """Resolve the effective connection for a datasource table."""
    connection = datasource.get('connection', {})
    if not connection and datasource.get('connections'):
        connection = datasource['connections'][0]
    connection_map = datasource.get('connection_map', {})
    table_details = table.get('connection_details', {})
    if table_details and table_details.get('type'):
        return table_details
    connection_name = table.get('connection')
    if connection_name and connection_name in connection_map:
        return connection_map[connection_name]
    return connection


def is_file_connection(connection):
    """Return whether a connection is a locally staged file source."""
    connection_type = str(connection.get('type', '')).strip().lower()
    return connection_type in FILE_SOURCE_TYPES


def datasource_has_connected_tables(datasource):
    """Return whether a datasource owns at least one connected table."""
    return any(
        not is_file_connection(table_connection(datasource, table))
        for table in datasource.get('tables', [])
    )


def _extract_context_tag(element):
    """Apply the extractor's two known object-model feature gates."""
    local = element.tag.rsplit('}', 1)[-1]
    for feature in ('ObjectModelEncapsulateLegacy', 'SpatialFileUnion'):
        prefix = '_.fcp.' + feature + '.'
        if local.startswith(prefix + 'false...'):
            return None
        if local.startswith(prefix + 'true...'):
            return local[len(prefix + 'true...'):]
    return local


def _reject_xml_declaration(*_args):
    raise SecurityError("DTD and entity declarations are not allowed in extract fragments")


def _safe_extract_fragment(value):
    """Reject DTDs anywhere, then use the shared bounded XML parser."""
    # The shared parser checks only the first 4096 characters for DTD/entity
    # patterns. Expat rejects real declarations even after a long prefix;
    # the shared parser may additionally reject declaration-like literal text.
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = _reject_xml_declaration
    parser.EntityDeclHandler = _reject_xml_declaration
    parser.ExternalEntityRefHandler = _reject_xml_declaration
    parser.Parse(value, True)
    return safe_parse_xml(value)


def _parse_extract_context(context):
    """Read retained XML fragments in memory, rejecting unsafe evidence."""
    if not isinstance(context, dict):
        raise ValueError("Malformed extract context")
    extracts = context.get('extract_xml')
    objects = context.get('object_extract_contexts_xml')
    if not isinstance(extracts, list) or not isinstance(objects, list):
        raise ValueError("Malformed extract context")
    if any(not isinstance(value, str) or not value for value in extracts + objects):
        raise ValueError("Malformed extract context")
    if sum(len(value) for value in extracts + objects) > MAX_XML_SIZE_MB * 1024 * 1024:
        raise ValueError("Extract context exceeds the XML size limit")
    try:
        extract_roots = [_safe_extract_fragment(value) for value in extracts]
        object_roots = [_safe_extract_fragment(value) for value in objects]
    except (SecurityError, expat.ExpatError, ElementTree.ParseError, UnicodeError, ValueError):
        raise ValueError("Unsafe or malformed extract context") from None
    if (any(_extract_context_tag(root) != 'extract' for root in extract_roots)
            or any(_extract_context_tag(root) != 'properties'
                   or root.get('context') != 'extract' for root in object_roots)):
        raise ValueError("Malformed extract context")
    return extract_roots, object_roots


def _native_extract_context(parsed):
    """Mirror the extractor's native/disabled decision, not its forced label."""
    extracts, objects = parsed
    return ((not extracts and bool(objects))
            or (len(extracts) == 1
                and extracts[0].get('enabled', '').lower() == 'false'))


def _context_mentions_hyper(parsed):
    """Find declared Hyper connections or members in active XML branches."""
    extracts, objects = parsed
    pending = extracts + objects
    while pending:
        element = pending.pop()
        tag = _extract_context_tag(element)
        if tag is None:
            continue
        if tag in ('connection', 'extract'):
            connector = str(element.get('class', '')).strip().lower()
            if connector in _HYPER_SOURCE_TYPES:
                return True
            if any(
                key.rsplit('}', 1)[-1] in ('dbname', 'filename', 'path')
                and value.lower().endswith('.hyper')
                for key, value in element.attrib.items()
            ):
                return True
        pending.extend(element)
    return False


def _native_context_covers_members(parsed_contexts, members):
    """Do the retained native/disabled contexts identify every declared member?"""
    if (not isinstance(members, list) or not members
            or any(not isinstance(member, str) or not member for member in members)):
        return False
    inactive_members = set()
    for parsed in parsed_contexts:
        if parsed is None or not _native_extract_context(parsed):
            continue
        extracts, objects = parsed
        for root in extracts + objects:
            for element in root.iter():
                tag = element.tag.rsplit('}', 1)[-1].rsplit('...', 1)[-1]
                if tag not in ('connection', 'extract'):
                    continue
                inactive_members.update(
                    value for key, value in element.attrib.items()
                    if key.rsplit('}', 1)[-1] in ('dbname', 'filename', 'path')
                    and value.lower().endswith('.hyper')
                )
    return set(members).issubset(inactive_members)


def _proven_native_selection(datasource, selection):
    """Accept the extractor's native DB decision only for database connectors."""
    if (selection.get('policy') != 'prefer_native_database'
            or selection.get('selected') != 'native_database'):
        return False
    members = selection.get('hyper_members')
    if (not isinstance(members, list) or not members
            or any(not isinstance(member, str) or not member for member in members)):
        return False
    for key in ('extract_xml', 'object_extract_contexts_xml'):
        values = selection.get(key, [])
        if (not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)):
            return False
    declared = datasource.get('hyper_source_members') or []
    if (not isinstance(declared, list)
            or any(not isinstance(member, str) or not member for member in declared)
            or not set(declared).issubset(members)):
        return False
    from tableau_export.m_query_builder import native_database_binding_issues

    def is_database(connection):
        return (isinstance(connection, dict)
                and isinstance(connection.get('details', {}), dict)
                and native_database_binding_issues(connection) == [])

    tables = datasource.get('tables') or []
    if not tables:
        connection = datasource.get('connection') or {}
        if not connection and datasource.get('connections'):
            connection = datasource['connections'][0]
        return is_database(connection)
    for table in tables:
        connection = table_connection(datasource, table)
        if table.get('hyper_extract_metadata') or not is_database(connection):
            return False
    return True


def _raise_unbound_hyper(datasource, reason, table=None):
    """Report source-level ambiguity without exposing paths or connection details."""
    raise PartialGenerationError(
        "Fabric cannot bind this active or unresolved Hyper snapshot to Notebook "
        f"ingestion: {reason}. A CSV, Excel, or database connection alone does "
        "not prove a complete catalog-verified export, a canonical "
        "Data/<table>.csv, and its physical-to-logical column mapping. "
        "Dataflow may otherwise emit an empty or sampled table, or ingest the "
        "original source instead of the selected snapshot. Independently "
        "verify the full export and lineage, then rebind the active datasource "
        "and table metadata to the CSV before generating the Fabric bundle.",
        item=(table.get('name') or '<unnamed table>') if table is not None else None,
        source=datasource.get('name') or datasource.get('caption'),
        recovery_kind='fabric_hyper_snapshot_ingestion',
        required_evidence=[
            'Exact Hyper physical identity and catalog validation',
            'Complete, unsampled export bound to canonical Data/<table>.csv',
            'Verified physical-to-logical column lineage without collisions',
            'Notebook-only Files/ ingestion of that canonical CSV',
        ],
    )


def _validated_extract_context(datasource, context, location):
    """Fail closed without putting fragment contents in the diagnostic."""
    try:
        return _parse_extract_context(context)
    except ValueError:
        _raise_unbound_hyper(
            datasource, f'the retained {location} extract context is unsafe or '
            'malformed or exceeds the XML size limit')


def preflight_fabric_ingestion(datasources):
    """Reject active Hyper inputs that the Fabric bundle cannot yet bind.

    An extractor's table-level ``hyper_identity`` selects an extract, even
    when its original connection is still CSV, Excel, or a database. It proves
    a declared identity, not that a complete catalog-backed export was mapped
    to the canonical CSV read by the Notebook. The current Fabric exporter and
    staging code do not establish that lineage. In particular, treating the
    original connection as the selected source or sending Hyper to Dataflow can
    silently load different data, an inline sample, or an empty table.

    Datasource-level materialization, source selection, and retained extract
    contexts must also be checked: ambiguous extraction can leave every table
    without an identity while preserving an active Hyper context. Declared
    members alone include inactive and unused history and are not an activation
    decision. Proven native or disabled extract contexts remain usable.
    A declared member without usable selection/activation evidence remains
    non-blocking, but is reported as an unverified source choice. Return the
    number of such datasources so callers can retain that diagnostic.
    No files are read, created, modified, or removed by this check.
    """
    unverified_hyper_sources = 0
    for datasource in datasources or ():
        if not datasource.get('tables'):
            connection = datasource.get('connection') or {}
            if not connection and datasource.get('connections'):
                connection = datasource['connections'][0]
            connector = (str(connection.get('type', '')).strip().lower()
                         if isinstance(connection, dict) else '')
            if connector in _HYPER_SOURCE_TYPES:
                _raise_unbound_hyper(
                    datasource, 'the Hyper datasource has no table identity or binding')
        selection = datasource.get('source_selection') or {}
        if not isinstance(selection, dict):
            selection = {}
        members = selection.get('hyper_members')
        contexts = selection.get('extract_xml') or selection.get('object_extract_contexts_xml')
        if datasource.get('materialized_extract') is not None:
            _raise_unbound_hyper(
                datasource, 'the materialized extract lacks a verified Fabric table binding')
        if datasource.get('hyper_extract_metadata') is not None:
            _raise_unbound_hyper(
                datasource, 'the datasource retains unbound Hyper field metadata')
        if ('extract_xml' in selection or 'object_extract_contexts_xml' in selection):
            _validated_extract_context(
                datasource, {
                    'extract_xml': selection.get('extract_xml', []),
                    'object_extract_contexts_xml': selection.get(
                        'object_extract_contexts_xml', []),
                }, 'source-selection')
        inactive_context = datasource.get('inactive_extract_context')
        inactive_parsed = (
            _validated_extract_context(datasource, inactive_context, 'inactive')
            if inactive_context is not None else None)
        relation_metadata = datasource.get('relation_metadata') or {}
        provenance = (relation_metadata.get('extract_context_provenance')
                      if isinstance(relation_metadata, dict) else None)
        provenance_parsed = (
            _validated_extract_context(datasource, provenance, 'physical-provenance')
            if provenance is not None else None)

        proven_native = _proven_native_selection(datasource, selection)
        selected = str(selection.get('selected', '')).strip().lower()
        if ((members and not proven_native)
                or selected in ('hyper', 'hyper_snapshot', 'extract', 'dataengine')):
            _raise_unbound_hyper(
                datasource, 'the Hyper selection has no proven native database decision '
                'or verified Fabric table binding')

        if provenance_parsed is not None and not proven_native:
            if (not _native_extract_context(provenance_parsed)
                    and (_context_mentions_hyper(provenance_parsed)
                         or datasource.get('hyper_source_members'))):
                _raise_unbound_hyper(
                    datasource, 'the retained Hyper extract context is active or '
                    'unresolved and has no verified Fabric table binding')

        native_selected = (
            proven_native
            and isinstance(members, list) and bool(members)
            and all(isinstance(member, str) and member for member in members)
            and isinstance(contexts, list) and bool(contexts)
            and all(isinstance(context, str) and context for context in contexts)
        )
        for table in datasource.get('tables', []):
            connection = table_connection(datasource, table) or {}
            if not isinstance(connection, dict):
                connection = {}
            connector = str(connection.get('type', '')).strip().lower()
            hyper_connector = connector in _HYPER_SOURCE_TYPES
            identity = table.get('hyper_identity')
            active_identity = identity is not None
            if not hyper_connector and not active_identity:
                continue
            historical_identity = (
                isinstance(identity, dict)
                and set(identity) == {'workbook', 'archive_member', 'schema', 'table'}
                and all(isinstance(value, str) and value
                        for value in identity.values())
                and identity['archive_member'] in (members or ())
                and identity['workbook'] == selection.get('workbook')
            )
            if (native_selected and not table.get('hyper_extract_metadata')
                    and historical_identity and not hyper_connector
                    and connector not in FILE_SOURCE_TYPES):
                # The source-selection label alone does not establish a
                # usable database connection.
                from tableau_export.m_query_builder import native_database_binding_issues
                if (isinstance(connection.get('details'), dict)
                        and native_database_binding_issues(connection) == []):
                    continue

            _raise_unbound_hyper(
                datasource, 'the table declares a Hyper identity or connector',
                table)

        if (datasource.get('hyper_source_members') and not proven_native
                and not _native_context_covers_members(
                    (inactive_parsed, provenance_parsed),
                    datasource['hyper_source_members'])):
            unverified_hyper_sources += 1

    if unverified_hyper_sources:
        logger.warning("%s Affected datasources: %d.",
                       HYPER_SOURCE_SELECTION_UNVERIFIED, unverified_hyper_sources)
    return unverified_hyper_sources
