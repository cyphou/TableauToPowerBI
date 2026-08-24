"""Unit tests for Sprint 132 — streaming JSON writes and large-workbook generator."""

import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))


class TestStreamingJson(unittest.TestCase):
    """Verify streaming JSON writer produces identical output to json.dump."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='stream_json_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_streaming_matches_standard_dump(self):
        """Streaming array write must produce valid JSON identical to json.dump."""
        from extract_tableau_data import TableauExtractor

        # Build a test array large enough to exercise streaming
        items = [{'id': i, 'name': f'Item_{i}', 'value': i * 1.5} for i in range(100)]

        # Standard write
        std_path = os.path.join(self.tmp, 'standard.json')
        with open(std_path, 'w', encoding='utf-8') as f:
            json.dump(items, f, indent=2, ensure_ascii=False)

        # Streaming write
        stream_path = os.path.join(self.tmp, 'streamed.json')
        TableauExtractor._stream_json_array(stream_path, items, default_fn=None)

        # Both must produce valid JSON
        with open(std_path, 'r', encoding='utf-8') as f:
            std_data = json.load(f)
        with open(stream_path, 'r', encoding='utf-8') as f:
            stream_data = json.load(f)

        self.assertEqual(std_data, stream_data)

    def test_streaming_empty_array(self):
        """Streaming an empty array must produce '[]'."""
        from extract_tableau_data import TableauExtractor

        path = os.path.join(self.tmp, 'empty.json')
        TableauExtractor._stream_json_array(path, [], default_fn=None)

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data, [])

    def test_streaming_single_item(self):
        """Streaming a single-item array must produce valid JSON."""
        from extract_tableau_data import TableauExtractor

        items = [{'key': 'value'}]
        path = os.path.join(self.tmp, 'single.json')
        TableauExtractor._stream_json_array(path, items, default_fn=None)

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data, items)

    def test_streaming_with_special_chars(self):
        """Streaming must handle unicode and special characters."""
        from extract_tableau_data import TableauExtractor

        items = [
            {'name': 'Café résumé', 'emoji': '🎉'},
            {'name': 'Line\nbreak', 'tab': 'col\there'},
        ]
        path = os.path.join(self.tmp, 'special.json')
        TableauExtractor._stream_json_array(path, items, default_fn=None)

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.assertEqual(data, items)

    def test_estimate_json_size_list(self):
        """Size estimation for a list must be reasonable."""
        from extract_tableau_data import TableauExtractor

        items = [{'x': i} for i in range(1000)]
        estimated = TableauExtractor._estimate_json_size(items)
        actual = len(json.dumps(items, ensure_ascii=False))
        # Estimation should be within 3x of actual
        self.assertGreater(estimated, actual * 0.3)
        self.assertLess(estimated, actual * 3)

    def test_estimate_json_size_empty(self):
        """Empty list estimation."""
        from extract_tableau_data import TableauExtractor
        self.assertEqual(TableauExtractor._estimate_json_size([]), 2)

    def test_save_extractions_uses_streaming_for_large(self):
        """save_extractions should use streaming for arrays > threshold."""
        from extract_tableau_data import TableauExtractor

        # Create a mock extractor with a large array
        ext = TableauExtractor.__new__(TableauExtractor)
        ext.output_dir = self.tmp

        # Build data that would trigger streaming (lower threshold for test)
        original_threshold = TableauExtractor._STREAM_THRESHOLD_BYTES
        try:
            TableauExtractor._STREAM_THRESHOLD_BYTES = 100  # Very low
            ext.workbook_data = {
                'test_items': [{'id': i, 'data': 'x' * 50} for i in range(50)]
            }
            ext.save_extractions()

            # Verify output is valid JSON
            out_path = os.path.join(self.tmp, 'test_items.json')
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
            self.assertEqual(len(loaded), 50)
        finally:
            TableauExtractor._STREAM_THRESHOLD_BYTES = original_threshold


class TestLargeWorkbookGenerator(unittest.TestCase):
    """Unit tests for the synthetic TWB generator."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='gen_twb_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generates_valid_xml(self):
        """Generated TWB must be valid XML."""
        import xml.etree.ElementTree as ET
        from tests.large_workbook_generator import generate_large_twb

        path = generate_large_twb(
            os.path.join(self.tmp, 'test.twb'),
            num_measures=10, num_worksheets=5, num_datasources=3, seed=1,
        )
        tree = ET.parse(path)
        root = tree.getroot()
        self.assertEqual(root.tag, 'workbook')

    def test_reproducible_with_seed(self):
        """Same seed must produce identical output."""
        from tests.large_workbook_generator import generate_large_twb

        p1 = generate_large_twb(
            os.path.join(self.tmp, 'a.twb'), num_measures=20, seed=99,
        )
        p2 = generate_large_twb(
            os.path.join(self.tmp, 'b.twb'), num_measures=20, seed=99,
        )
        with open(p1) as f1, open(p2) as f2:
            self.assertEqual(f1.read(), f2.read())

    def test_different_seeds_differ(self):
        """Different seeds must produce different output."""
        from tests.large_workbook_generator import generate_large_twb

        p1 = generate_large_twb(
            os.path.join(self.tmp, 'a.twb'), num_measures=20, seed=1,
        )
        p2 = generate_large_twb(
            os.path.join(self.tmp, 'b.twb'), num_measures=20, seed=2,
        )
        with open(p1) as f1, open(p2) as f2:
            self.assertNotEqual(f1.read(), f2.read())

    def test_stats_report(self):
        """get_twb_stats must return sensible counts."""
        from tests.large_workbook_generator import generate_large_twb, get_twb_stats

        path = generate_large_twb(
            os.path.join(self.tmp, 'stats.twb'),
            num_measures=50, num_worksheets=10, num_datasources=5,
            num_dashboards=3, seed=42,
        )
        stats = get_twb_stats(path)
        self.assertGreater(stats['file_size_bytes'], 0)
        self.assertEqual(stats['worksheets'], 10)
        self.assertEqual(stats['dashboards'], 3)
        self.assertGreaterEqual(stats['calculations'], 50)

    def test_parameters_generated(self):
        """Parameters must appear in generated TWB."""
        import xml.etree.ElementTree as ET
        from tests.large_workbook_generator import generate_large_twb

        path = generate_large_twb(
            os.path.join(self.tmp, 'params.twb'),
            num_measures=10, num_parameters=5, seed=42,
        )
        tree = ET.parse(path)
        params = tree.getroot().findall('.//column[@param-domain-type]')
        self.assertEqual(len(params), 5)


if __name__ == '__main__':
    unittest.main()
