"""Sprint 181 — Enterprise Connector Expansion tests.

Verifies the new enterprise database connectors (Dremio, ClickHouse,
SingleStore, Firebolt, Starburst) and the depth variants (IBM Db2,
Teradata, Azure Synapse) generate valid Power Query M, are registered
in the dispatch table, and are auto-detected from Tableau connection
classes.
"""

import pytest

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'tableau_export'))

from tableau_export.m_query_builder import (
    _M_GENERATORS,
    generate_power_query_m,
    _gen_m_dremio,
    _gen_m_clickhouse,
    _gen_m_singlestore,
    _gen_m_firebolt,
    _gen_m_starburst,
    _gen_m_db2_depth,
    _gen_m_teradata_depth,
    _gen_m_synapse_depth,
)
from tableau_export.datasource_extractor import _build_from_dispatch


def _m(conn_type, details, table='MyTable', columns=None):
    return generate_power_query_m(
        {'type': conn_type, 'details': details},
        {'name': table, 'columns': columns or []},
    )


# ── Registration ─────────────────────────────────────────────────────────────

class TestRegistration:
    @pytest.mark.parametrize('alias', [
        'Dremio', 'dremio',
        'ClickHouse', 'clickhouse',
        'SingleStore', 'singlestore', 'MemSQL',
        'Firebolt', 'firebolt',
        'Starburst', 'starburst', 'Starburst Galaxy', 'Trino Enterprise',
        'Db2 Deep', 'IBM Db2 Deep',
        'Teradata Deep',
        'Synapse Deep', 'Azure Synapse Deep',
    ])
    def test_alias_registered(self, alias):
        assert alias in _M_GENERATORS

    def test_target_connector_count(self):
        # v39.0.0 target: 75+ connector aliases
        assert len(_M_GENERATORS) >= 75


# ── Dremio ───────────────────────────────────────────────────────────────────

class TestDremio:
    def test_default(self):
        m = _gen_m_dremio({'server': 'dremio.local', 'port': '31010', 'schema': 'space'}, 'Sales', [])
        assert m.startswith('let')
        assert 'Dremio Connector' in m
        assert 'HOST=dremio.local' in m
        assert 'PORT=31010' in m
        assert '#"Sales Table"' in m

    def test_custom_sql(self):
        m = _gen_m_dremio({'server': 'd', 'custom_sql': 'SELECT * FROM foo'}, 'Q', [])
        assert 'Odbc.Query' in m
        assert 'SELECT * FROM foo' in m

    def test_dispatch(self):
        m = _m('Dremio', {'server': 's', 'schema': 'sp'})
        assert 'Dremio' in m


# ── ClickHouse ───────────────────────────────────────────────────────────────

class TestClickHouse:
    def test_default(self):
        m = _gen_m_clickhouse({'server': 'ch', 'port': '8123', 'database': 'analytics'}, 'Events', [])
        assert 'ClickHouse ODBC Driver (Unicode)' in m
        assert 'SERVER=ch' in m
        assert 'DATABASE=analytics' in m
        assert '#"Events Table"' in m

    def test_custom_sql(self):
        m = _gen_m_clickhouse({'server': 'ch', 'custom_sql': 'SELECT 1'}, 'Q', [])
        assert 'Odbc.Query' in m
        assert 'SELECT 1' in m

    def test_default_database(self):
        m = _gen_m_clickhouse({'server': 'ch'}, 'T', [])
        assert 'DATABASE=default' in m


# ── SingleStore ──────────────────────────────────────────────────────────────

class TestSingleStore:
    def test_default(self):
        m = _gen_m_singlestore({'server': 'ss', 'port': '3306', 'database': 'memsql'}, 'Orders', [])
        assert 'MySQL.Database' in m
        assert 'ss:3306' in m

    def test_memsql_alias(self):
        m = _m('MemSQL', {'server': 'ss', 'database': 'db'})
        assert 'MySQL.Database' in m

    def test_dispatch(self):
        m = _m('SingleStore', {'server': 'ss', 'database': 'db'})
        assert 'MySQL.Database' in m


# ── Firebolt ─────────────────────────────────────────────────────────────────

class TestFirebolt:
    def test_default(self):
        m = _gen_m_firebolt({'server': 'eng.firebolt.io', 'database': 'mydb'}, 'Facts', [])
        assert 'Firebolt ODBC' in m
        assert 'ENGINE=eng.firebolt.io' in m
        assert 'DATABASE=mydb' in m
        assert '#"Facts Table"' in m

    def test_custom_sql(self):
        m = _gen_m_firebolt({'server': 'e', 'custom_sql': 'SELECT a'}, 'Q', [])
        assert 'Odbc.Query' in m
        assert 'SELECT a' in m


# ── Starburst ────────────────────────────────────────────────────────────────

class TestStarburst:
    def test_default(self):
        m = _gen_m_starburst(
            {'server': 'sb.example.com', 'port': '443', 'catalog': 'hive', 'schema': 'sales'},
            'Lineitem', [])
        assert 'Starburst ODBC Driver' in m
        assert 'HOST=sb.example.com' in m
        assert 'SSL=1' in m
        assert '#"hive"' in m
        assert '#"sales"' in m
        assert '#"Lineitem Table"' in m

    def test_custom_sql(self):
        m = _gen_m_starburst({'server': 'sb', 'custom_sql': 'SELECT * FROM t'}, 'Q', [])
        assert 'Odbc.Query' in m
        assert 'SELECT * FROM t' in m

    def test_galaxy_alias(self):
        m = _m('Starburst Galaxy', {'server': 'sb', 'catalog': 'c', 'schema': 's'})
        assert 'Starburst ODBC Driver' in m

    def test_trino_enterprise_alias(self):
        m = _m('Trino Enterprise', {'server': 'sb', 'catalog': 'c', 'schema': 's'})
        assert 'Starburst ODBC Driver' in m


# ── Db2 depth ────────────────────────────────────────────────────────────────

class TestDb2Depth:
    def test_default(self):
        m = _gen_m_db2_depth(
            {'server': 'db2host', 'port': '50000', 'database': 'SAMPLE', 'schema': 'DB2INST1'},
            'EMPLOYEE', [])
        assert 'DB2.Database' in m
        assert 'db2host:50000' in m
        assert '#"DB2INST1"' in m
        assert '#"EMPLOYEE Table"' in m

    def test_custom_sql(self):
        m = _gen_m_db2_depth({'server': 'h', 'database': 'D', 'custom_sql': 'SELECT 1'}, 'Q', [])
        assert 'Query="SELECT 1"' in m


# ── Teradata depth ───────────────────────────────────────────────────────────

class TestTeradataDepth:
    def test_default(self):
        m = _gen_m_teradata_depth({'server': 'td.example.com', 'database': 'DBC'}, 'Sales', [])
        assert 'Teradata.Database' in m
        assert 'td.example.com' in m
        assert '#"DBC"' in m
        assert '#"Sales Table"' in m

    def test_custom_sql(self):
        m = _gen_m_teradata_depth({'server': 'td', 'custom_sql': 'SEL * FROM t'}, 'Q', [])
        assert 'Query="SEL * FROM t"' in m


# ── Synapse depth ────────────────────────────────────────────────────────────

class TestSynapseDepth:
    def test_default(self):
        m = _gen_m_synapse_depth(
            {'server': 'ws.sql.azuresynapse.net', 'database': 'pool', 'schema': 'dbo'},
            'Fact', [])
        assert 'Sql.Database' in m
        assert 'ws.sql.azuresynapse.net' in m
        assert 'Schema="dbo"' in m
        assert 'Item="Fact"' in m

    def test_custom_sql(self):
        m = _gen_m_synapse_depth({'server': 's', 'database': 'd', 'custom_sql': 'SELECT 1'}, 'Q', [])
        assert 'Query="SELECT 1"' in m


# ── Auto-detection via dispatch spec ─────────────────────────────────────────

class TestAutoDetection:
    @pytest.mark.parametrize('xml_attrs,expected_type', [
        ({'server': 's', 'port': '31010', 'schema': 'sp'}, 'Dremio'),
        ({'server': 's', 'port': '8123', 'dbname': 'd'}, 'ClickHouse'),
        ({'server': 's', 'dbname': 'd'}, 'SingleStore'),
        ({'server': 's', 'dbname': 'd'}, 'Firebolt'),
        ({'server': 's', 'catalog': 'c'}, 'Starburst'),
    ])
    def test_dispatch_builds_type(self, xml_attrs, expected_type):
        spec_map = {
            'Dremio': ('Dremio', {'server': ('server', ''), 'port': ('port', '31010'),
                                  'schema': ('schema', ''), 'username': ('username', '')}),
            'ClickHouse': ('ClickHouse', {'server': ('server', ''), 'port': ('port', '8123'),
                                          'database': ('dbname', 'default'), 'username': ('username', '')}),
            'SingleStore': ('SingleStore', {'server': ('server', ''), 'port': ('port', '3306'),
                                            'database': ('dbname', ''), 'username': ('username', '')}),
            'Firebolt': ('Firebolt', {'server': ('server', ''), 'database': ('dbname', ''),
                                      'username': ('username', '')}),
            'Starburst': ('Starburst', {'server': ('server', ''), 'port': ('port', '443'),
                                        'catalog': ('catalog', ''), 'schema': ('schema', 'default'),
                                        'username': ('username', '')}),
        }
        result = _build_from_dispatch(xml_attrs, spec_map[expected_type])
        assert result['type'] == expected_type
        assert result['details']['server'] == 's'

    def test_built_details_drive_generation(self):
        spec = ('ClickHouse', {'server': ('server', ''), 'port': ('port', '8123'),
                               'database': ('dbname', 'default')})
        built = _build_from_dispatch({'server': 'ch', 'dbname': 'metrics'}, spec)
        m = generate_power_query_m(built, {'name': 'T', 'columns': []})
        assert 'SERVER=ch' in m
        assert 'DATABASE=metrics' in m


# ── M validity smoke ─────────────────────────────────────────────────────────

class TestMValidity:
    @pytest.mark.parametrize('gen,details', [
        (_gen_m_dremio, {'server': 's', 'schema': 'sp'}),
        (_gen_m_clickhouse, {'server': 's', 'database': 'd'}),
        (_gen_m_singlestore, {'server': 's', 'database': 'd'}),
        (_gen_m_firebolt, {'server': 's', 'database': 'd'}),
        (_gen_m_starburst, {'server': 's', 'catalog': 'c', 'schema': 'sc'}),
        (_gen_m_db2_depth, {'server': 's', 'database': 'd', 'schema': 'sc'}),
        (_gen_m_teradata_depth, {'server': 's', 'database': 'd'}),
        (_gen_m_synapse_depth, {'server': 's', 'database': 'd', 'schema': 'dbo'}),
    ])
    def test_balanced_let_in(self, gen, details):
        m = gen(details, 'Tbl', [])
        assert m.count('let') >= 1
        assert m.rstrip().endswith('Result') or m.rstrip().endswith('Source')
        assert m.startswith('let')
