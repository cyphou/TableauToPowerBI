"""Unit tests for tableau_export.safe_xml helper functions."""

import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))

from safe_xml import (
    ExtractionWarning,
    ExtractionWarningCode,
    safe_find,
    safe_findall,
    safe_findtext,
    safe_get_attr,
)
from extract_tableau_data import TableauExtractor


class TestSafeXmlHelpers(unittest.TestCase):
    def test_safe_get_attr_present(self):
        elem = ET.fromstring("<x a='1'/>")
        self.assertEqual(safe_get_attr(elem, 'a', ''), '1')

    def test_safe_get_attr_missing_default(self):
        elem = ET.fromstring('<x/>')
        self.assertEqual(safe_get_attr(elem, 'a', 'd'), 'd')

    def test_safe_get_attr_none(self):
        self.assertEqual(safe_get_attr(None, 'a', 'd'), 'd')

    def test_safe_find_present(self):
        elem = ET.fromstring('<root><a/></root>')
        self.assertIsNotNone(safe_find(elem, 'a'))

    def test_safe_find_none(self):
        self.assertIsNone(safe_find(None, 'a'))

    def test_safe_findall_present(self):
        elem = ET.fromstring('<root><a/><a/></root>')
        self.assertEqual(len(safe_findall(elem, 'a')), 2)

    def test_safe_findall_none(self):
        self.assertEqual(safe_findall(None, 'a'), [])

    def test_safe_findtext_present(self):
        elem = ET.fromstring('<root><a>hello</a></root>')
        self.assertEqual(safe_findtext(elem, 'a', ''), 'hello')

    def test_safe_findtext_none(self):
        self.assertEqual(safe_findtext(None, 'a', 'd'), 'd')

    def test_warning_dataclass(self):
        w = ExtractionWarning(
            code=ExtractionWarningCode.UNSAFE_ZIP_ENTRY.value,
            message='x',
            context='y',
        )
        data = w.as_dict()
        self.assertEqual(data['code'], 'unsafe_zip_entry')
        self.assertEqual(data['context'], 'y')


class TestExtractorWarnings(unittest.TestCase):
    def test_zip_traversal_records_warning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            twbx_path = os.path.join(tmpdir, 'warn.twbx')
            with zipfile.ZipFile(twbx_path, 'w') as zf:
                zf.writestr('../../evil.twb', '<workbook/>')
                zf.writestr('ok.twb', '<workbook/>')

            ext = TableauExtractor(twbx_path, output_dir=tmpdir)
            xml = ext.read_tableau_file()
            self.assertIsNotNone(xml)
            codes = {w.get('code') for w in ext.extraction_warnings}
            self.assertIn('unsafe_zip_entry', codes)


if __name__ == '__main__':
    unittest.main()
