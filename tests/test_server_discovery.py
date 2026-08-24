"""Tests for Sprint 161-162 — server_client enhancements."""

import unittest
from unittest.mock import patch, MagicMock
from tableau_export.server_client import TableauServerClient


class TestServerDiscovery(unittest.TestCase):
    """Test Sprint 161 server discovery methods."""

    def setUp(self):
        """Create client instance without connecting."""
        self.client = TableauServerClient(
            server_url='https://tableau.example.com',
            token_name='test-token',
            token_secret='secret',
            site_id='TestSite',
        )
        self.client._auth_token = 'fake-token'
        self.client._site_luid = 'site-123'

    @patch.object(TableauServerClient, 'get_workbook_connections')
    @patch.object(TableauServerClient, 'list_workbooks')
    @patch.object(TableauServerClient, 'list_views')
    def test_get_workbook_dependencies(self, mock_views, mock_wbs, mock_conns):
        """Dependency graph resolves datasource connections."""
        mock_conns.return_value = [
            {'datasource': {'id': 'ds-1', 'name': 'Sales'}},
        ]
        mock_wbs.return_value = [
            {'id': 'wb-1'},
            {'id': 'wb-2'},
        ]
        mock_views.return_value = [
            {'id': 'v-1', 'workbook': {'id': 'wb-1'}},
        ]

        result = self.client.get_workbook_dependencies('wb-1')
        self.assertIn('ds-1', result['datasources'])
        self.assertIn('v-1', result['views'])

    @patch.object(TableauServerClient, 'list_views')
    def test_get_usage_stats(self, mock_views):
        """Usage stats aggregate view counts."""
        mock_views.return_value = [
            {
                'id': 'v-1',
                'workbook': {'id': 'wb-1'},
                'usage': {'totalViewCount': 150},
                'updatedAt': '2024-01-15',
            },
            {
                'id': 'v-2',
                'workbook': {'id': 'wb-1'},
                'usage': {'totalViewCount': 50},
                'updatedAt': '2024-02-01',
            },
        ]
        result = self.client.get_usage_stats('wb-1')
        self.assertEqual(result['totalViews'], 200)
        self.assertEqual(result['viewCount'], 2)

    @patch.object(TableauServerClient, '_request')
    def test_get_permissions(self, mock_request):
        """Permissions parse granteeCapabilities."""
        mock_request.return_value = {
            'permissions': {
                'granteeCapabilities': [
                    {
                        'user': {'id': 'u-1', 'name': 'alice'},
                        'capabilities': {
                            'capability': [
                                {'name': 'Read', 'mode': 'Allow'},
                                {'name': 'Write', 'mode': 'Deny'},
                            ]
                        },
                    }
                ]
            }
        }
        result = self.client.get_permissions('wb-1')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['granteeType'], 'user')
        self.assertEqual(result[0]['granteeName'], 'alice')
        self.assertEqual(result[0]['capabilities']['Read'], 'Allow')

    @patch.object(TableauServerClient, '_request')
    def test_get_quality_warnings(self, mock_request):
        """Quality warnings returned as list."""
        mock_request.return_value = {
            'dataQualityWarnings': {
                'dataQualityWarning': [
                    {'type': 'WARNING', 'message': 'Stale data'},
                ]
            }
        }
        result = self.client.get_quality_warnings('workbook', 'wb-1')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['type'], 'WARNING')

    @patch.object(TableauServerClient, '_request')
    def test_get_quality_warnings_api_unavailable(self, mock_request):
        """Missing API returns empty list."""
        mock_request.side_effect = Exception('404 Not Found')
        result = self.client.get_quality_warnings()
        self.assertEqual(result, [])


class TestCloudDetection(unittest.TestCase):
    """Test Sprint 162 Tableau Cloud detection."""

    def test_detect_cloud(self):
        """Cloud domain detected correctly."""
        client = TableauServerClient(
            server_url='https://10ax.online.tableau.com',
            token_name='t', token_secret='s', site_id='site',
        )
        self.assertEqual(client.detect_cloud_vs_server(), 'cloud')

    def test_detect_server(self):
        """On-premise URL detected as server."""
        client = TableauServerClient(
            server_url='https://tableau.internal.company.com',
            token_name='t', token_secret='s', site_id='site',
        )
        self.assertEqual(client.detect_cloud_vs_server(), 'server')

    def test_detect_eu_cloud(self):
        """EU region Cloud domain detected."""
        client = TableauServerClient(
            server_url='https://eu-west-1a.online.tableau.com',
            token_name='t', token_secret='s', site_id='site',
        )
        self.assertEqual(client.detect_cloud_vs_server(), 'cloud')


class TestJWTAuth(unittest.TestCase):
    """Test JWT authentication method."""

    @patch.object(TableauServerClient, '_request')
    def test_sign_in_jwt_success(self, mock_request):
        """JWT sign-in sets auth token."""
        mock_request.return_value = {
            'credentials': {
                'token': 'jwt-auth-token-123',
                'site': {'id': 'site-456', 'contentUrl': 'mysite'},
            }
        }
        client = TableauServerClient(
            server_url='https://10ax.online.tableau.com',
            token_name='t', token_secret='s', site_id='mysite',
        )
        result = client.sign_in_jwt('eyJ...')
        self.assertTrue(result)
        self.assertEqual(client._auth_token, 'jwt-auth-token-123')
        self.assertEqual(client._site_luid, 'site-456')

    @patch.object(TableauServerClient, '_request')
    def test_sign_in_jwt_failure(self, mock_request):
        """JWT sign-in failure returns False."""
        mock_request.side_effect = Exception('401 Unauthorized')
        client = TableauServerClient(
            server_url='https://10ax.online.tableau.com',
            token_name='t', token_secret='s', site_id='mysite',
        )
        result = client.sign_in_jwt('invalid-jwt')
        self.assertFalse(result)


class TestMetadataGraphQL(unittest.TestCase):
    """Test Metadata API GraphQL queries."""

    @patch.object(TableauServerClient, '_request')
    def test_graphql_query(self, mock_request):
        """GraphQL query returns data field."""
        mock_request.return_value = {
            'data': {
                'workbooks': [{'name': 'Sales', 'id': 'wb-1'}]
            }
        }
        client = TableauServerClient(
            server_url='https://tableau.example.com',
            token_name='t', token_secret='s', site_id='site',
        )
        client._auth_token = 'token'
        result = client.get_metadata_graphql(
            'query { workbooks { name id } }')
        self.assertIn('workbooks', result)

    @patch.object(TableauServerClient, '_request')
    def test_graphql_failure_returns_empty(self, mock_request):
        """GraphQL failure returns empty dict."""
        mock_request.side_effect = Exception('Metadata API disabled')
        client = TableauServerClient(
            server_url='https://tableau.example.com',
            token_name='t', token_secret='s', site_id='site',
        )
        client._auth_token = 'token'
        result = client.get_metadata_graphql('query { workbooks { name } }')
        self.assertEqual(result, {})

    @patch.object(TableauServerClient, '_request')
    def test_lineage_upstream(self, mock_request):
        """Lineage upstream returns structured data."""
        mock_request.return_value = {
            'data': {
                'workbooks': [{
                    'upstreamDatasources': [
                        {'name': 'SalesDS', 'id': 'ds-1',
                         'upstreamTables': []}
                    ],
                    'upstreamTables': [
                        {'name': 'orders', 'fullName': 'public.orders',
                         'database': {'name': 'salesdb',
                                      'connectionType': 'postgres'}},
                    ],
                }]
            }
        }
        client = TableauServerClient(
            server_url='https://tableau.example.com',
            token_name='t', token_secret='s', site_id='site',
        )
        client._auth_token = 'token'
        result = client.get_lineage_upstream('wb-1')
        self.assertIn('salesdb', result['databases'])
        self.assertEqual(len(result['tables']), 1)
        self.assertEqual(len(result['datasources']), 1)


class TestServerSummary(unittest.TestCase):
    """Test comprehensive server summary."""

    @patch.object(TableauServerClient, 'get_site_info')
    @patch.object(TableauServerClient, 'list_workbooks')
    @patch.object(TableauServerClient, 'list_datasources')
    @patch.object(TableauServerClient, 'list_users')
    @patch.object(TableauServerClient, 'list_groups')
    @patch.object(TableauServerClient, 'list_projects')
    @patch.object(TableauServerClient, 'list_schedules')
    @patch.object(TableauServerClient, 'list_prep_flows')
    def test_summary_aggregates(self, mock_flows, mock_scheds, mock_projs,
                                 mock_groups, mock_users, mock_ds, mock_wbs,
                                 mock_site):
        """Summary aggregates all inventory counts."""
        mock_site.return_value = {'name': 'TestSite'}
        mock_wbs.return_value = [{'id': '1'}, {'id': '2'}]
        mock_ds.return_value = [{'id': '1'}]
        mock_users.return_value = [{'id': '1'}, {'id': '2'}, {'id': '3'}]
        mock_groups.return_value = [{'id': '1'}]
        mock_projs.return_value = [{'id': '1'}, {'id': '2'}]
        mock_scheds.return_value = [{'id': '1'}]
        mock_flows.return_value = []

        client = TableauServerClient(
            server_url='https://tableau.example.com',
            token_name='t', token_secret='s', site_id='site',
        )
        client._auth_token = 'token'
        client._site_luid = 's'

        result = client.get_server_summary()
        self.assertEqual(result['workbook_count'], 2)
        self.assertEqual(result['user_count'], 3)
        self.assertEqual(result['prep_flow_count'], 0)


if __name__ == '__main__':
    unittest.main()


