"""Tests for Sprint 155 (Cloud Connectors) and Sprint 158-160 (DAX extensions)."""

import unittest
from tableau_export.m_query_builder import generate_power_query_m
from tableau_export.dax_converter import (
    convert_regexp_match,
    convert_regexp_replace,
    convert_regexp_extract,
    convert_spatial_to_python_visual,
    convert_window_percentile,
    convert_running_with_partition,
    convert_lookup_offset,
    convert_nested_lod,
    convert_multi_dim_exclude,
    convert_ismemberof_to_rls,
)


class TestCloudConnectors(unittest.TestCase):
    """Test Sprint 155 cloud/SaaS connector generators."""

    def _gen(self, conn_type, details, table_name='TestTable'):
        """Helper to call generate_power_query_m with proper structure."""
        connection = {'type': conn_type, 'details': details}
        table = {'name': table_name, 'columns': []}
        return generate_power_query_m(connection, table)

    def test_servicenow_connector(self):
        """ServiceNow generates OData.Feed M query."""
        result = self._gen('ServiceNow', {
            'server': 'company.service-now.com',
        }, 'incident')
        self.assertIn('OData.Feed', result)
        self.assertIn('service-now.com', result)

    def test_databricks_unity_connector(self):
        """Databricks Unity Catalog generates Databricks.Catalogs."""
        result = self._gen('Databricks Unity', {
            'server': 'adb-12345.azuredatabricks.net',
            'catalog': 'main',
            'schema': 'default',
        }, 'sales_fact')
        self.assertIn('Databricks.Catalogs', result)
        self.assertIn('adb-12345', result)

    def test_denodo_connector(self):
        """Denodo generates ODBC connection."""
        result = self._gen('Denodo', {
            'server': 'denodo.company.com',
            'port': '9996',
            'database': 'analytics',
        }, 'v_customers')
        self.assertIn('Odbc.DataSource', result)
        self.assertIn('denodo.company.com', result)

    def test_essbase_connector(self):
        """Essbase generates XMLA/ODBC bridge."""
        result = self._gen('essbase', {
            'server': 'essbase.company.com',
            'database': 'Sample',
        }, 'Basic')
        self.assertIn('Odbc.DataSource', result)

    def test_splunk_connector(self):
        """Splunk generates Web.Contents REST query."""
        result = self._gen('Splunk', {
            'server': 'splunk.company.com',
            'port': '8089',
        }, 'main')
        self.assertIn('Web.Contents', result)
        self.assertIn('splunk.company.com', result)

    def test_sap_hana_deep_connector(self):
        """SAP HANA Deep generates schema navigation."""
        result = self._gen('SAP HANA Deep', {
            'server': 'hana.company.com:30015',
            'schema': 'SALES',
        }, 'ORDERS')
        self.assertIn('SapHana.Database', result)
        self.assertIn('SALES', result)

    def test_redshift_deep_connector(self):
        """Redshift Deep generates schema + Spectrum support."""
        result = self._gen('Redshift Deep', {
            'server': 'cluster.us-east-1.redshift.amazonaws.com',
            'database': 'warehouse',
            'schema': 'analytics',
        }, 'events')
        self.assertIn('AmazonRedshift.Database', result)

    def test_unknown_connector_fallback(self):
        """Unknown connector type returns generic M placeholder."""
        result = self._gen('SomeUnknownDB', {
            'server': 'db.example.com',
        }, 'data')
        # Should still produce valid M (generic fallback)
        self.assertIn('let', result.lower())


class TestRegexpConversion(unittest.TestCase):
    """Test Sprint 158 regex/spatial conversions."""

    def test_regexp_match_known_pattern(self):
        """Known regex pattern (email) converts to CONTAINSSTRING."""
        result = convert_regexp_match(
            'REGEXP_MATCH([Email], "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}")')
        self.assertIn('CONTAINSSTRING', result)

    def test_regexp_match_simple_literal(self):
        """Simple literal pattern returns CONTAINSSTRING."""
        result = convert_regexp_match(
            'REGEXP_MATCH([Field], "ABC123")')
        self.assertIn('CONTAINSSTRING', result)

    def test_regexp_replace_literal(self):
        """Literal replacement uses SUBSTITUTE."""
        result = convert_regexp_replace(
            'REGEXP_REPLACE([Name], "old", "new")')
        self.assertIn('SUBSTITUTE', result)

    def test_regexp_extract_basic(self):
        """Extract returns MID approximation."""
        result = convert_regexp_extract(
            'REGEXP_EXTRACT([Code], "(\\d+)")')
        self.assertIn('MID', result)

    def test_spatial_to_python_visual(self):
        """Spatial functions generate Python visual template."""
        result = convert_spatial_to_python_visual(
            'MAKEPOINT([Lat], [Lon])')
        self.assertIn('geopandas', result['script'])
        self.assertIn('matplotlib', result['script'])


class TestTableCalcConversion(unittest.TestCase):
    """Test Sprint 159 table calculation depth."""

    def test_window_percentile(self):
        """WINDOW_PERCENTILE converts to PERCENTILEX.INC."""
        result = convert_window_percentile(
            'WINDOW_PERCENTILE(SUM([Sales]), 0.75)', 'Orders')
        self.assertIn('PERCENTILEX.INC', result)
        self.assertIn("'Orders'", result)

    def test_running_with_partition(self):
        """RUNNING_SUM with partition uses CALCULATE+FILTER."""
        result = convert_running_with_partition(
            'RUNNING_SUM(SUM([Revenue]))', 'Sales',
            partition_cols=['Region'])
        self.assertIn('CALCULATE', result)
        self.assertIn('FILTER', result)
        self.assertIn('[Region]', result)

    def test_lookup_offset(self):
        """LOOKUP maps to OFFSET function."""
        result = convert_lookup_offset(
            'LOOKUP(SUM([Profit]), -1)', 'Sales')
        self.assertIn('OFFSET', result)


class TestLodAndSecurity(unittest.TestCase):
    """Test Sprint 160 LOD and security conversions."""

    def test_nested_lod(self):
        """Nested LOD → nested CALCULATE."""
        result = convert_nested_lod(
            '{FIXED [Region] : SUM({FIXED [Category] : AVG([Discount])})}',
            'Orders',
            all_dimensions=['Region', 'Category'])
        self.assertIn('CALCULATE', result)

    def test_multi_dim_exclude(self):
        """Multi-dimension EXCLUDE uses REMOVEFILTERS."""
        result = convert_multi_dim_exclude(
            '{EXCLUDE [Region], [State] : SUM([Sales])}',
            'Orders', ['Region', 'State', 'City'])
        self.assertIn('REMOVEFILTERS', result)
        self.assertIn('[Region]', result)
        self.assertIn('[State]', result)

    def test_ismemberof_to_rls(self):
        """ISMEMBEROF → TRUE() + RLS annotation."""
        result = convert_ismemberof_to_rls(
            'ISMEMBEROF("Finance Team")')
        self.assertIn('TRUE()', result['dax'])
        self.assertEqual(result['rls_roles'][0]['group'], 'Finance Team')


if __name__ == '__main__':
    unittest.main()
