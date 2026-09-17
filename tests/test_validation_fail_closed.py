"""Guard against fail-open validation.

The DAX and M validators are in-package and stdlib-only, so an import failure
means a broken install — never a missing optional dependency. Wrapping them in
``try/except`` with a stub that returns ``[]`` would make every expression look
valid and silently disable the healing and openability gates.
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Modules whose correctness depends on the validators actually running.
_GATE_MODULES = [
    'powerbi_import/openability.py',
    'powerbi_import/autoheal.py',
    'powerbi_import/tmdl_self_heal.py',
]

_VALIDATORS = {'validate_dax_expression', 'validate_m_query'}


def _guarded_validator_imports(path):
    """Return validator names imported inside a try/except in *path*."""
    tree = ast.parse(open(path, encoding='utf-8').read())
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.ImportFrom):
                for alias in sub.names:
                    if alias.name in _VALIDATORS:
                        guarded.add(alias.name)
    return guarded


class TestValidatorsAreNotFailOpen(unittest.TestCase):
    def test_gate_modules_import_validators_unguarded(self):
        for rel in _GATE_MODULES:
            path = os.path.join(_ROOT, rel)
            with self.subTest(module=rel):
                self.assertTrue(os.path.isfile(path), f"missing {rel}")
                guarded = _guarded_validator_imports(path)
                self.assertEqual(
                    guarded, set(),
                    f"{rel} imports {sorted(guarded)} inside try/except; a stub "
                    "returning [] would declare every expression valid")

    def test_validators_are_stdlib_only(self):
        """They must stay dependency-free so the unguarded import is safe."""
        for rel in ('powerbi_import/dax_validator.py',
                    'powerbi_import/m_validator.py'):
            tree = ast.parse(open(os.path.join(_ROOT, rel), encoding='utf-8').read())
            roots = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots.update(a.name.split('.')[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    roots.add(node.module.split('.')[0])
            with self.subTest(module=rel):
                self.assertTrue(
                    roots <= {'re', 'typing'},
                    f"{rel} gained dependencies {sorted(roots - {'re', 'typing'})}; "
                    "unguarded imports in the gate modules assume it stays stdlib-only")


class TestValidatorsRejectBadInput(unittest.TestCase):
    def test_dax_validator_reports_unbalanced(self):
        from powerbi_import.dax_validator import validate_dax_expression
        self.assertNotEqual(validate_dax_expression('SUM([A]'), [])

    def test_dax_validator_accepts_valid(self):
        from powerbi_import.dax_validator import validate_dax_expression
        self.assertEqual(validate_dax_expression("SUM('T'[A])"), [])

    def test_m_validator_reports_unbalanced(self):
        from powerbi_import.m_validator import validate_m_query
        self.assertNotEqual(validate_m_query('let x = (1 in x'), [])


if __name__ == '__main__':
    unittest.main()
