"""Power BI refuses to open a .pbip whose paths exceed its own limits.

Desktop shreds a project through PBIProjectUtils.EnsureNotLong and fails with
"The specified path, file name, or both are too long" -- a load failure, not a
warning. Two things guard against it: table filenames are capped so we never
create the overflow ourselves, and an openability check reports it when the
user's own output folder is deep enough to blow the limit anyway.
"""
import os
import tempfile
import unittest

from powerbi_import.openability import check_openability
from powerbi_import.tmdl_writers import PBI_MAX_PATH, _safe_filename


def _deep_dir(length):
    """Build an absolute directory path of roughly *length* characters."""
    base = os.path.abspath(os.sep + 'x')
    parts = []
    while len(base) + len(os.sep.join(parts)) + len(os.sep) < length:
        parts.append('d' * 20)
    return base + os.sep + os.sep.join(parts)


class TestSafeFilenameCapping(unittest.TestCase):

    def test_short_name_is_untouched(self):
        d = _deep_dir(60)
        self.assertEqual(_safe_filename('Orders', d, '.tmdl'), 'Orders')

    def test_no_base_dir_keeps_legacy_behaviour(self):
        name = 'Extract (' + 'a' * 200 + ')'
        self.assertEqual(_safe_filename(name), name)

    def test_illegal_characters_still_replaced(self):
        self.assertEqual(_safe_filename('a/b:c*d'), 'a_b_c_d')

    def test_long_name_under_deep_dir_is_capped(self):
        d = _deep_dir(200)
        name = 'Extract (TABLEAU_FRET_caract des flux (Recap region_LS _v2))' * 3
        stem = _safe_filename(name, d, '.tmdl')
        full = os.path.join(os.path.abspath(d), stem + '.tmdl')
        self.assertLessEqual(len(full), PBI_MAX_PATH)
        # Without the cap the same name would have overflowed.
        raw = os.path.join(os.path.abspath(d), _safe_filename(name) + '.tmdl')
        self.assertGreater(len(raw), PBI_MAX_PATH)

    def test_capping_is_deterministic(self):
        d = _deep_dir(200)
        name = 'Extract ' + 'z' * 150
        self.assertEqual(_safe_filename(name, d, '.tmdl'),
                         _safe_filename(name, d, '.tmdl'))

    def test_distinct_long_names_stay_distinct(self):
        d = _deep_dir(200)
        a = _safe_filename('Extract ' + 'z' * 150 + ' one', d, '.tmdl')
        b = _safe_filename('Extract ' + 'z' * 150 + ' two', d, '.tmdl')
        self.assertNotEqual(a, b)

    def test_hopeless_budget_falls_back_to_hash(self):
        # A directory that leaves no room at all still yields a usable stem.
        d = _deep_dir(PBI_MAX_PATH - 4)
        stem = _safe_filename('Extract ' + 'z' * 150, d, '.tmdl')
        self.assertTrue(stem)
        self.assertNotIn(os.sep, stem)


class TestPathLengthCheck(unittest.TestCase):

    def _project(self, root):
        sm = os.path.join(root, 'P.SemanticModel', 'definition')
        os.makedirs(sm)
        with open(os.path.join(sm, 'model.tmdl'), 'w', encoding='utf-8') as f:
            f.write('model Model\n')
        return root

    def test_normal_project_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            self._project(d)
            names = {c.name for c in check_openability(d).checks}
            self.assertIn('path_length', names)
            check = [c for c in check_openability(d).checks
                     if c.name == 'path_length'][0]
            self.assertTrue(check.ok)

    def test_over_long_file_blocks_open(self):
        with tempfile.TemporaryDirectory() as d:
            self._project(d)
            deep = os.path.join(d, 'P.Report', 'definition', 'pages')
            # Nest until the directory itself passes Power BI's 248 limit, so
            # the visual.json inside it also passes the 260 file limit.
            while len(deep) < PBI_MAX_PATH - 8:
                deep = os.path.join(deep, 'section' + 'n' * 12)
            target = os.path.join(deep, 'visual.json')
            self.assertGreater(len(target), PBI_MAX_PATH)
            try:
                os.makedirs(deep)
                with open(target, 'w', encoding='utf-8') as f:
                    f.write('{}')
            except OSError:
                self.skipTest('OS refuses paths this long; '
                              'long path support is disabled')
            result = check_openability(d)
            check = [c for c in result.checks if c.name == 'path_length'][0]
            self.assertFalse(check.ok)
            self.assertTrue(any('path_length' in b
                                for b in result.blocking_issues))


if __name__ == '__main__':
    unittest.main()
