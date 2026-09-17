"""Tests for Sprint 157 — Hyper reader extensions."""

import os
import unittest
import tempfile
from unittest.mock import patch, MagicMock
from tableau_export.hyper_reader import (
    detect_tde_format,
    discover_multi_table_hyper,
    read_hyper_streaming,
    extract_hyper_filters,
)


class TestDetectTdeFormat(unittest.TestCase):
    """Test TDE vs Hyper format detection."""

    def test_hyper_file_detected(self):
        """File with SQLite header detected as Hyper."""
        with tempfile.NamedTemporaryFile(suffix='.hyper', delete=False) as f:
            f.write(b'SQLite format 3\x00' + b'\x00' * 48)
            f.flush()
            result = detect_tde_format(f.name)
        os.unlink(f.name)
        self.assertFalse(result['is_tde'])
        self.assertEqual(result['format_version'], 'Hyper')

    def test_tde_file_detected(self):
        """File with TDE magic bytes detected as legacy TDE."""
        with tempfile.NamedTemporaryFile(suffix='.tde', delete=False) as f:
            f.write(b'\x00\x00\x00\x00' + b'\x00' * 60)
            f.flush()
            result = detect_tde_format(f.name)
        os.unlink(f.name)
        self.assertTrue(result['is_tde'])
        self.assertIn('Legacy TDE', result['migration_note'])

    def test_nonexistent_file(self):
        """Nonexistent file returns appropriate message."""
        result = detect_tde_format('/nonexistent/file.hyper')
        self.assertFalse(result['is_tde'])
        self.assertEqual(result['format_version'], 'unknown')

    def test_unknown_format(self):
        """Unrecognized header returns unknown."""
        with tempfile.NamedTemporaryFile(suffix='.dat', delete=False) as f:
            f.write(b'RANDOM HEADER DATA' + b'\xff' * 46)
            f.flush()
            result = detect_tde_format(f.name)
        os.unlink(f.name)
        self.assertFalse(result['is_tde'])
        self.assertEqual(result['format_version'], 'unknown')


class TestDiscoverMultiTableHyper(unittest.TestCase):
    """Test multi-table discovery (mocked — no actual Hyper files)."""

    @patch('sqlite3.connect')
    def test_sqlite_fallback_discovery(self, mock_connect):
        """SQLite fallback discovers tables."""
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        # First call: table list
        mock_cursor.fetchall.side_effect = [
            [('Extract',)],  # table names
            [(0, 'ID', 'INTEGER', 0, None, 0),
             (1, 'Name', 'TEXT', 0, None, 0)],  # PRAGMA
        ]
        mock_cursor.fetchone.return_value = (100,)  # COUNT(*)

        # Force ImportError on tableauhyperapi so sqlite fallback is used
        import sys
        saved = sys.modules.get('tableauhyperapi')
        sys.modules['tableauhyperapi'] = None
        try:
            result = discover_multi_table_hyper('/fake/file.hyper')
        finally:
            if saved is None:
                sys.modules.pop('tableauhyperapi', None)
            else:
                sys.modules['tableauhyperapi'] = saved

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['table'], 'Extract')
        self.assertEqual(result[0]['row_count'], 100)
        self.assertEqual(len(result[0]['columns']), 2)

    def test_nonexistent_file_returns_empty(self):
        """Missing file returns empty list (no crash)."""
        result = discover_multi_table_hyper('/nonexistent/file.hyper')
        self.assertEqual(result, [])


class TestReadHyperStreaming(unittest.TestCase):
    """Test streaming batch reader."""

    def test_no_tableauhyperapi_no_crash(self):
        """Without tableauhyperapi, yields nothing gracefully."""
        batches = list(read_hyper_streaming('/fake/file.hyper'))
        self.assertEqual(batches, [])


class TestExtractHyperFilters(unittest.TestCase):
    """Test extract filter extraction from TWB XML."""

    def test_categorical_filter(self):
        """Categorical extract filter parsed to M expression."""
        import xml.etree.ElementTree as ET
        xml_str = '''
        <workbook>
          <datasource name="Sales">
            <extract>
              <filter column="[Region]">
                <member>East</member>
                <member>West</member>
              </filter>
            </extract>
          </datasource>
        </workbook>
        '''
        root = ET.fromstring(xml_str)
        filters = extract_hyper_filters(root, 'Sales')
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]['column'], 'Region')
        self.assertEqual(filters[0]['operator'], 'in')
        self.assertIn('East', filters[0]['values'])
        self.assertIn('Table.SelectRows', filters[0]['m_filter'])

    def test_range_filter(self):
        """Range extract filter parsed with min/max."""
        import xml.etree.ElementTree as ET
        xml_str = '''
        <workbook>
          <datasource name="Orders">
            <extract>
              <filter column="[Amount]" min="100" max="5000">
              </filter>
            </extract>
          </datasource>
        </workbook>
        '''
        root = ET.fromstring(xml_str)
        filters = extract_hyper_filters(root, 'Orders')
        self.assertEqual(len(filters), 1)
        self.assertEqual(filters[0]['column'], 'Amount')
        self.assertEqual(filters[0]['operator'], 'range')
        self.assertIn('Table.SelectRows', filters[0]['m_filter'])

    def test_no_filters_found(self):
        """Missing datasource returns empty list."""
        import xml.etree.ElementTree as ET
        xml_str = '<workbook><datasource name="Other"></datasource></workbook>'
        root = ET.fromstring(xml_str)
        filters = extract_hyper_filters(root, 'NonExistent')
        self.assertEqual(filters, [])


if __name__ == '__main__':
    unittest.main()
