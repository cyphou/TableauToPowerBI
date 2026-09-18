"""What the generation path actually depends on in ``visual_generator``.

The module reads as the visual builder, and its tests reinforce that: 38 of its
55 top-level functions are named by a test while no migration ever calls them.
Production imports ten names from it — three lookup maps and seven helpers —
and builds its visuals in ``pbip_generator``. Pinning that surface means the
day someone wires a builder in is a decision, not an accident.
"""

import ast
import glob
import os
import unittest

_REPO = os.path.join(os.path.dirname(__file__), '..')
_MODULE = 'visual_generator'

#: Everything production imports from visual_generator today.
EXPECTED_SURFACE = {
    'APPROXIMATION_MAP',
    'VISUAL_DATA_ROLES',
    'VISUAL_TYPE_MAP',
    '_build_dynamic_reference_line',
    '_build_motion_chart_bookmarks',
    'clear_auto_generated_measures',
    'generate_script_visual',
    'get_approximation_note',
    'resolve_custom_visual_type',
    'resolve_visual_type',
}


def _production_files():
    pattern = os.path.join(_REPO, 'powerbi_import', '**', '*.py')
    for path in glob.glob(pattern, recursive=True):
        if os.path.basename(path) != f'{_MODULE}.py':
            yield path


def _imported_names():
    found = {}
    for path in _production_files():
        # utf-8-sig: a stray BOM once made this whole file invisible to ast.
        tree = ast.parse(open(path, encoding='utf-8-sig').read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                    node.module or '').endswith(_MODULE):
                for alias in node.names:
                    found.setdefault(alias.name, set()).add(
                        os.path.basename(path))
    return found


class TestVisualGeneratorSurface(unittest.TestCase):

    def test_production_imports_only_the_declared_surface(self):
        self.assertEqual(EXPECTED_SURFACE, set(_imported_names()))

    def test_every_declared_name_exists_in_the_module(self):
        module_path = os.path.join(_REPO, 'powerbi_import', f'{_MODULE}.py')
        tree = ast.parse(open(module_path, encoding='utf-8-sig').read())
        defined = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
        self.assertEqual(set(), EXPECTED_SURFACE - defined)

    def test_the_surface_is_not_empty(self):
        # A scan that silently finds nothing would pass the equality above
        # only if EXPECTED_SURFACE were emptied too; guard both at once.
        self.assertTrue(_imported_names())


class TestNoByteOrderMark(unittest.TestCase):
    """A BOM is legal Python but invisible to naive tooling, and was."""

    def test_no_source_file_starts_with_a_bom(self):
        offenders = []
        for folder in ('powerbi_import', 'tableau_export', 'tests'):
            for path in glob.glob(os.path.join(_REPO, folder, '**', '*.py'),
                                  recursive=True):
                with open(path, 'rb') as handle:
                    if handle.read(3) == b'\xef\xbb\xbf':
                        offenders.append(os.path.relpath(path, _REPO))
        self.assertEqual([], offenders)


if __name__ == '__main__':
    unittest.main()
