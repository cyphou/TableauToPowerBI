"""The field-name lint must catch the mismatch that started all this.

The inventory read `worksheet` off actions while the extractor writes
`source_worksheets`, and nothing failed. These tests drive the analyser with
synthetic modules so its judgement is verifiable without running a migration.
"""

import os
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.check_field_names import (
    EXTERNAL_CONSUMER_FIELDS, find_unknown, scan_module,
)


def _module(source):
    handle = tempfile.NamedTemporaryFile('w', suffix='.py', delete=False,
                                         encoding='utf-8')
    handle.write(textwrap.dedent(source))
    handle.close()
    return handle.name


class _AnalyserCase(unittest.TestCase):
    def groups(self, source):
        path = _module(source)
        self.addCleanup(os.unlink, path)
        return {(kind, keys) for kind, keys, _line in scan_module(path)}


class TestTracksExtractedRecords(_AnalyserCase):
    def test_key_read_in_a_loop_over_extracted(self):
        groups = self.groups("""
            def f(extracted):
                for action in extracted.get('actions', []):
                    return action.get('worksheet')
        """)
        self.assertIn(('actions', frozenset({'worksheet'})), groups)

    def test_key_read_through_an_intermediate_variable(self):
        groups = self.groups("""
            def f(extracted):
                actions = extracted.get('actions', [])
                for action in actions:
                    return action.get('worksheet')
        """)
        self.assertIn(('actions', frozenset({'worksheet'})), groups)

    def test_subscript_read(self):
        groups = self.groups("""
            def f(extracted):
                for ws in extracted['worksheets']:
                    return ws['name']
        """)
        self.assertIn(('worksheets', frozenset({'name'})), groups)


class TestGroupsFallbackChains(_AnalyserCase):
    """A working fallback is not a defect; only a value with no source is."""

    def test_or_chain_is_one_value(self):
        groups = self.groups("""
            def f(extracted):
                for ws in extracted.get('worksheets', []):
                    return ws.get('chart_type') or ws.get('mark_type')
        """)
        self.assertIn(('worksheets', frozenset({'chart_type', 'mark_type'})), groups)

    def test_default_argument_chain_is_one_value(self):
        groups = self.groups("""
            def f(extracted):
                for uf in extracted.get('user_filters', []):
                    return uf.get('table', uf.get('datasource', 'default'))
        """)
        self.assertIn(('user_filters', frozenset({'table', 'datasource'})), groups)


class TestScoping(_AnalyserCase):
    def test_a_binding_does_not_leak_into_another_function(self):
        """A module-wide pass labelled unrelated locals as extracted records."""
        groups = self.groups("""
            def consumer(extracted):
                for ws in extracted.get('worksheets', []):
                    return ws.get('name')

            def unrelated():
                for ws in build_comparison_rows():
                    return ws.get('tableau')
        """)
        self.assertNotIn(('worksheets', frozenset({'tableau'})), groups)


class TestVerdict(unittest.TestCase):
    VOCAB = {'actions': {'name', 'source_worksheets'},
             'worksheets': {'name', 'chart_type'}}

    def setUp(self):
        self.path = _module("""
            def f(extracted):
                for action in extracted.get('actions', []):
                    return action.get('worksheet')
        """)
        self.addCleanup(os.unlink, self.path)

    def _unknown(self, declared=None):
        import scripts.check_field_names as lint
        original = lint.glob.glob
        lint.glob.glob = lambda pattern, **kw: (
            [self.path] if 'powerbi_import' in pattern else original(pattern, **kw))
        try:
            return lint.find_unknown(self.VOCAB, declared or set())
        finally:
            lint.glob.glob = original

    def test_reports_a_key_with_no_source(self):
        unknown = self._unknown()
        self.assertEqual(len(unknown), 1)
        _module_name, kind, keys, _line = unknown[0]
        self.assertEqual(kind, 'actions')
        self.assertEqual(keys, frozenset({'worksheet'}))

    def test_a_key_the_extractor_assigns_is_accepted(self):
        """Observation proves presence, never absence — declared keys count."""
        self.assertEqual(self._unknown(declared={'worksheet'}), [])

    def test_a_documented_external_fallback_is_accepted(self):
        path = _module("""
            def f(extracted):
                for ws in extracted.get('worksheets', []):
                    return ws.get('dynamic_visibility')
        """)
        self.addCleanup(os.unlink, path)
        self.path = path
        self.VOCAB = {'worksheets': {'name'}}
        self.assertEqual(self._unknown(), [])
        self.assertIn(('worksheets', 'dynamic_visibility'),
                      EXTERNAL_CONSUMER_FIELDS)

    def test_external_fallback_does_not_accept_an_unknown_key(self):
        path = _module("""
            def f(extracted):
                for ws in extracted.get('worksheets', []):
                    return ws.get('invented_field')
        """)
        self.addCleanup(os.unlink, path)
        self.path = path
        self.VOCAB = {'worksheets': {'name'}}
        unknown = self._unknown()
        self.assertEqual(frozenset({'invented_field'}), unknown[0][2])

    def test_underscore_prefixed_fields_are_enrichment_not_extraction(self):
        path = _module("""
            def f(extracted):
                for calc in extracted.get('calculations', []):
                    return calc.get('_merge_action')
        """)
        self.addCleanup(os.unlink, path)
        self.path = path
        self.VOCAB = {'calculations': {'name'}}
        self.assertEqual(self._unknown(), [])


if __name__ == '__main__':
    unittest.main()
