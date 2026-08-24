"""Desktop openability suite for real migrated fixtures.

This suite validates, without launching Power BI Desktop, that generated PBIP
projects pass the static openability gate after real extraction/generation.

Run explicitly:
    $env:RUN_OPENABILITY_SUITE = "1"
    ./.venv/Scripts/python.exe -m pytest tests/test_desktop_openability_suite.py -v
"""

import glob
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

from tableau_export.extract_tableau_data import TableauExtractor
from powerbi_import.import_to_powerbi import PowerBIImporter
from powerbi_import.openability import check_openability


_RUN_OPENABILITY_SUITE = os.environ.get('RUN_OPENABILITY_SUITE') == '1'


def _sample(name):
    return os.path.join(ROOT, 'examples', 'tableau_samples', name)


@unittest.skipUnless(
    _RUN_OPENABILITY_SUITE,
    'Desktop openability suite requires RUN_OPENABILITY_SUITE=1',
)
class TestDesktopOpenabilitySuite(unittest.TestCase):
    """Openability checks against real migrated fixtures."""

    def _extract(self, workbook_path, output_dir):
        extractor = TableauExtractor(workbook_path, output_dir=output_dir)
        ok = extractor.extract_all()
        self.assertTrue(ok, f'Extraction failed: {workbook_path}')

    def _generate_single_project(self, workbook_path, output_root):
        extract_dir = tempfile.mkdtemp(prefix='openability_extract_')
        try:
            self._extract(workbook_path, extract_dir)
            report_name = os.path.splitext(os.path.basename(workbook_path))[0]
            importer = PowerBIImporter(source_dir=extract_dir)
            importer.import_all(
                generate_pbip=True,
                report_name=report_name,
                output_dir=output_root,
                output_format='pbip',
            )
            candidates = [
                os.path.dirname(p)
                for p in glob.glob(os.path.join(output_root, '**', '*.pbip'), recursive=True)
            ]
            self.assertTrue(candidates, 'No PBIP project generated for single-workbook flow')
            return sorted(set(candidates))[-1]
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)

    def test_single_fixture_project_is_openable(self):
        workbook = _sample('Superstore_Sales.twb')
        if not os.path.isfile(workbook):
            self.skipTest(f'Fixture not found: {workbook}')

        output_root = tempfile.mkdtemp(prefix='openability_single_')
        try:
            project_dir = self._generate_single_project(workbook, output_root)
            report = check_openability(project_dir)
            self.assertTrue(
                report.openable,
                f'Single fixture not openable: {report.blocking_issues}',
            )
        finally:
            shutil.rmtree(output_root, ignore_errors=True)

    def test_shared_model_project_is_openable(self):
        workbook_a = _sample('Superstore_Sales.twb')
        workbook_b = _sample('HR_Analytics.twb')
        if not os.path.isfile(workbook_a) or not os.path.isfile(workbook_b):
            self.skipTest('Shared-model fixtures not found')

        run_dir = tempfile.mkdtemp(prefix='openability_shared_')
        extract_a = os.path.join(run_dir, 'extract_a')
        extract_b = os.path.join(run_dir, 'extract_b')
        output_root = os.path.join(run_dir, 'out')
        os.makedirs(extract_a, exist_ok=True)
        os.makedirs(extract_b, exist_ok=True)
        os.makedirs(output_root, exist_ok=True)

        try:
            self._extract(workbook_a, extract_a)
            self._extract(workbook_b, extract_b)

            importer_a = PowerBIImporter(source_dir=extract_a)
            importer_b = PowerBIImporter(source_dir=extract_b)
            converted_a = importer_a._load_converted_objects()
            converted_b = importer_b._load_converted_objects()

            result = importer_a.import_shared_model(
                model_name='DesktopOpenabilityShared',
                all_converted_objects=[converted_a, converted_b],
                workbook_names=['Superstore_Sales', 'HR_Analytics'],
                output_dir=output_root,
                output_format='pbip',
                force_merge=True,
            )
            self.assertIsNotNone(result)
            self.assertIn('model_path', result)
            model_path = result.get('model_path')
            self.assertTrue(model_path and os.path.isdir(model_path))

            # Quality guard: shared semantic model should not be empty.
            manifest_path = os.path.join(os.path.dirname(model_path), 'merge_manifest.json')
            self.assertTrue(os.path.isfile(manifest_path), 'merge_manifest.json not generated')
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            table_count = manifest.get('artifact_counts', {}).get('tables', 0)
            self.assertGreater(
                table_count,
                0,
                f'Shared semantic model is empty (tables={table_count})',
            )

            project_dir = os.path.dirname(model_path)
            report = check_openability(project_dir)
            self.assertTrue(
                report.openable,
                f'Shared-model fixture not openable: {report.blocking_issues}',
            )
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
