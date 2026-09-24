"""Guard against patching a name that the target module only re-exports.

`patch.object(tmdl_generator, '_write_table_tmdl')` stopped intercepting when
both that function and its caller moved to `tmdl_writers`. The patch still
succeeded -- the attribute was there -- and simply changed nothing. That is the
repository's most persistent defect shape: a check that runs and proves nothing.

The rule is narrow on purpose. Patching an imported name is correct when the
module *calls* it; `patch(llm_client, 'urlopen')` is how a dependency gets
stubbed. Only a name the module imports and never uses is a pure re-export.
"""

import ast
import os
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.check_patch_targets import (  # noqa: E402
    KNOWN_OFFENDERS,
    _module_facts,
    _patch_targets,
    find_offenders,
)


class TestDetectorJudgement(unittest.TestCase):
    """Synthetic modules, so the rule is checkable without the real tree."""

    def _facts(self, source, tmpdir):
        path = os.path.join(tmpdir, 'm.py')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(textwrap.dedent(source))
        return _module_facts(path)

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_called_import_is_used(self):
        defined, imported, used = self._facts("""
            from urllib.request import urlopen

            def fetch(u):
                return urlopen(u)
        """, self.tmp)
        self.assertIn('urlopen', imported)
        self.assertIn('urlopen', used)
        self.assertNotIn('urlopen', defined)

    def test_a_pure_reexport_is_not_used(self):
        defined, imported, used = self._facts("""
            from other import helper
        """, self.tmp)
        self.assertIn('helper', imported)
        self.assertNotIn('helper', used)

    def test_an_attribute_access_counts_as_use(self):
        _defined, _imported, used = self._facts("""
            import json

            def dump(x):
                return json.dumps(x)
        """, self.tmp)
        self.assertIn('json', used)

    def test_patch_object_is_recognised(self):
        tree = ast.parse(textwrap.dedent("""
            import powerbi_import.tmdl_generator as tg
            with patch.object(tg, '_write_table_tmdl'):
                pass
        """))
        found = list(_patch_targets(tree, {'tmdl_generator': None}))
        self.assertEqual(found[0][1:], ('tmdl_generator', '_write_table_tmdl'))

    def test_dotted_patch_string_is_recognised(self):
        tree = ast.parse(
            "with patch('powerbi_import.tmdl_generator._write_table_tmdl'):\n    pass\n")
        found = list(_patch_targets(tree, {'tmdl_generator': None}))
        self.assertEqual(found[0][1:], ('tmdl_generator', '_write_table_tmdl'))


class TestRealTestSuite(unittest.TestCase):
    def test_no_test_patches_a_pure_reexport(self):
        offenders = [o for o in find_offenders()
                     if f"{o[0]}:{o[3]}" not in KNOWN_OFFENDERS]
        self.assertEqual(
            offenders, [],
            "these patches intercept nothing:\n  "
            + "\n  ".join(f"{t}:{ln} patches {m}.{a}, which {m} only re-exports"
                          for t, ln, m, a in offenders))

    def test_the_scan_actually_reaches_the_tests(self):
        """A path slip would empty the result and make the check vacuous."""
        test_root = os.path.join(os.path.dirname(__file__))
        names = [n for n in os.listdir(test_root)
                 if n.startswith('test_') and n.endswith('.py')]
        self.assertGreater(len(names), 200)


if __name__ == '__main__':
    unittest.main()
