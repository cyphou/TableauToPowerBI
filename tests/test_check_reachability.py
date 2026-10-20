"""Exercise import scanning without blessing unreachable production modules."""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import check_reachability as scanner


class TestCheckReachability(unittest.TestCase):
    def test_desktop_script_is_runnable_from_outside_the_repository(self):
        path = os.path.join(scanner._REPO_ROOT,
                            'scripts', 'compare_desktop_reports.py')
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run(
                [sys.executable, path, '--help'], cwd=cwd,
                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--screen-comparison', result.stdout)
        self.assertIn('source_dir', result.stdout)
        self.assertIn('output_root', result.stdout)

    def test_desktop_script_really_imports_comparison_module(self):
        path = os.path.join(scanner._REPO_ROOT,
                            'scripts', 'compare_desktop_reports.py')
        self.assertIn('powerbi_import.desktop_comparison',
                      scanner._imported_names(path, ''))

    def test_desktop_script_import_closure_reaches_all_four_modules(self):
        # This is evidence for declaring the real external caller, not a
        # baseline exception or a patch to the production entrypoint registry.
        with patch.object(scanner, 'CLI_ENTRY',
                          'scripts/compare_desktop_reports.py'), \
                patch.object(scanner, 'DECLARED_ENTRY_POINTS', {}):
            _, reached, _ = scanner.analyse()
        for name in ('desktop_comparison', 'desktop_navigation',
                     'local_pixel_comparison', 'tableau_desktop_probe'):
            with self.subTest(module=name):
                self.assertIn('powerbi_import.' + name, reached)

    def test_bom_relative_and_absolute_imports_are_scanned(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'module.py')
            with open(path, 'w', encoding='utf-8-sig') as handle:
                handle.write('import powerbi_import.desktop_comparison\n'
                             'from . import desktop_navigation\n'
                             'from .local_pixel_comparison import compare_pngs\n')
            self.assertEqual(scanner._imported_names(path, 'powerbi_import'), {
                'powerbi_import.desktop_comparison',
                'powerbi_import',
                'powerbi_import.desktop_navigation',
                'powerbi_import.local_pixel_comparison',
                'powerbi_import.local_pixel_comparison.compare_pngs',
            })

    def test_unimported_module_is_not_reached_from_a_script(self):
        with tempfile.TemporaryDirectory() as root:
            paths = {}
            for name, source in (
                    ('entry', 'from powerbi_import import used\n'),
                    ('powerbi_import', ''),
                    ('powerbi_import.used', ''),
                    ('powerbi_import.unused', '')):
                path = os.path.join(root, name + '.py')
                with open(path, 'w', encoding='utf-8') as handle:
                    handle.write(source)
                paths[name] = path
            self.assertEqual(scanner.reachable_modules(paths, {'entry'}), {
                'entry', 'powerbi_import', 'powerbi_import.used'})
