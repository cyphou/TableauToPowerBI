"""Source routing helpers for Fabric-native ingestion artifacts."""

from __future__ import annotations


FILE_SOURCE_TYPES = {'csv', 'excel'}


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