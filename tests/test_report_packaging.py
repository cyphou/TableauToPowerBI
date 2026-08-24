"""Tests for Sprint 175 — PDF/PPTX Report Export.

Covers:
- pdf_renderer: print-optimized HTML generation
- pptx_report: PPTX executive summary generation
- report_packager: ZIP package bundling
"""

import json
import os
import tempfile
import unittest
import zipfile


class TestPdfRenderer(unittest.TestCase):
    """Tests for powerbi_import.pdf_renderer."""

    def test_render_print_html_basic(self):
        from powerbi_import.pdf_renderer import render_print_html

        html = "<html><head><title>Original</title><style>body{}</style></head><body><h1>Test</h1></body></html>"
        result = render_print_html(html, title="Test Report")
        self.assertIn("@media print", result)
        self.assertIn("Test Report", result)
        self.assertIn("print-mode", result)

    def test_render_print_html_expands_collapsed_sections(self):
        from powerbi_import.pdf_renderer import render_print_html

        html = '<div class="section-body collapsed">Content</div>'
        result = render_print_html(html, title="Expand Test")
        self.assertNotIn('class="section-body collapsed"', result)
        self.assertIn('class="section-body"', result)

    def test_render_print_html_shows_all_tabs(self):
        from powerbi_import.pdf_renderer import render_print_html

        html = '<html><body><div class="tab-content" style="display:none">Tab 2</div></body></html>'
        result = render_print_html(html, title="Tab Test")
        # CSS .print-mode .tab-content { display: block !important } overrides inline style
        self.assertIn('class="tab-content active"', result)

    def test_render_print_html_adds_banner(self):
        from powerbi_import.pdf_renderer import render_print_html

        html = "<html><body><p>Hello</p></body></html>"
        result = render_print_html(html, title="Banner Test")
        self.assertIn("Save as PDF", result)
        self.assertIn("Ctrl", result)

    def test_render_print_html_default_title(self):
        from powerbi_import.pdf_renderer import render_print_html

        html = "<html><body></body></html>"
        result = render_print_html(html)
        # No title override — original HTML has no <title>, so none injected
        self.assertIn("@media print", result)
        self.assertIn("print-mode", result)

    def test_save_print_html(self):
        from powerbi_import.pdf_renderer import save_print_html

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pdf.html")
            html = "<html><body>Test</body></html>"
            result = save_print_html(html, path)
            self.assertEqual(result, os.path.abspath(path))
            self.assertTrue(os.path.exists(path))
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.assertEqual(content, html)

    def test_save_print_html_creates_dirs(self):
        from powerbi_import.pdf_renderer import save_print_html

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "subdir", "nested", "report.pdf.html")
            save_print_html("<html></html>", path)
            self.assertTrue(os.path.exists(path))

    def test_print_css_has_a4(self):
        from powerbi_import.pdf_renderer import _PRINT_CSS

        self.assertIn("A4", _PRINT_CSS)

    def test_print_css_has_color_adjust(self):
        from powerbi_import.pdf_renderer import _PRINT_CSS

        self.assertIn("print-color-adjust", _PRINT_CSS)

    def test_render_print_html_preserves_body_content(self):
        from powerbi_import.pdf_renderer import render_print_html

        html = "<html><body><table><tr><td>Data</td></tr></table></body></html>"
        result = render_print_html(html, title="Data Test")
        self.assertIn("<table>", result)
        self.assertIn("Data", result)


class TestPptxReport(unittest.TestCase):
    """Tests for powerbi_import.pptx_report."""

    def _make_assessment_data(self, score='GREEN', categories=None):
        if categories is None:
            categories = [
                {
                    'name': 'Data Sources',
                    'worst_severity': 'pass',
                    'total_checks': 3, 'total_pass': 3,
                    'total_warn': 0, 'total_fail': 0,
                    'checks': [
                        {'name': 'Check 1', 'severity': 'pass',
                         'detail': 'OK', 'recommendation': ''},
                    ],
                },
                {
                    'name': 'Calculations',
                    'worst_severity': 'warn',
                    'total_checks': 5, 'total_pass': 3,
                    'total_warn': 2, 'total_fail': 0,
                    'checks': [
                        {'name': 'LOD Check', 'severity': 'warn',
                         'detail': 'Complex LOD', 'recommendation': 'Review DAX'},
                    ],
                },
            ]
        return {
            'workbook_name': 'Test Workbook',
            'timestamp': '2025-01-15T10:30:00Z',
            'overall_score': score,
            'totals': {
                'checks': 8, 'pass': 6, 'warn': 2, 'fail': 0,
            },
            'categories': categories,
        }

    def test_generate_pptx_creates_file(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            result = generate_pptx_report(self._make_assessment_data(), path)
            self.assertEqual(result, os.path.abspath(path))
            self.assertTrue(os.path.exists(path))

    def test_pptx_is_valid_zip(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(), path)
            with zipfile.ZipFile(path, 'r') as zf:
                names = zf.namelist()
                self.assertIn('[Content_Types].xml', names)
                self.assertIn('ppt/presentation.xml', names)

    def test_pptx_has_5_slides(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(), path)
            with zipfile.ZipFile(path, 'r') as zf:
                slides = [n for n in zf.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml')]
                self.assertEqual(len(slides), 5)

    def test_pptx_green_score(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(score='GREEN'), path)
            with zipfile.ZipFile(path, 'r') as zf:
                slide1 = zf.read('ppt/slides/slide1.xml').decode('utf-8')
                self.assertIn('GREEN', slide1)

    def test_pptx_yellow_score(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(score='YELLOW'), path)
            with zipfile.ZipFile(path, 'r') as zf:
                slide1 = zf.read('ppt/slides/slide1.xml').decode('utf-8')
                self.assertIn('YELLOW', slide1)

    def test_pptx_red_score(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(score='RED'), path)
            with zipfile.ZipFile(path, 'r') as zf:
                slide1 = zf.read('ppt/slides/slide1.xml').decode('utf-8')
                self.assertIn('RED', slide1)

    def test_pptx_creates_dirs(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "report.pptx")
            generate_pptx_report(self._make_assessment_data(), path)
            self.assertTrue(os.path.exists(path))

    def test_pptx_with_migration_stats(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            stats = {'visuals_converted': 12, 'measures_converted': 8}
            generate_pptx_report(self._make_assessment_data(), path,
                                 migration_stats=stats)
            self.assertTrue(os.path.exists(path))

    def test_pptx_empty_categories(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            data = self._make_assessment_data(categories=[])
            generate_pptx_report(data, path)
            self.assertTrue(os.path.exists(path))

    def test_pptx_many_categories(self):
        from powerbi_import.pptx_report import generate_pptx_report

        cats = []
        for i in range(15):
            cats.append({
                'name': f'Category {i}',
                'worst_severity': 'pass',
                'total_checks': 1, 'total_pass': 1,
                'total_warn': 0, 'total_fail': 0,
                'checks': [],
            })
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(categories=cats), path)
            self.assertTrue(os.path.exists(path))

    def test_pptx_content_types_xml(self):
        from powerbi_import.pptx_report import generate_pptx_report

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "report.pptx")
            generate_pptx_report(self._make_assessment_data(), path)
            with zipfile.ZipFile(path, 'r') as zf:
                ct = zf.read('[Content_Types].xml').decode('utf-8')
                self.assertIn('application/vnd.openxmlformats', ct)


class TestReportPackager(unittest.TestCase):
    """Tests for powerbi_import.report_packager."""

    class _MockReport:
        def __init__(self, name='TestWorkbook'):
            self.workbook_name = name
            self.total_checks = 10
            self.total_pass = 7
            self.total_warn = 2
            self.total_fail = 1
            self.categories = []
            self.overall_score = 'YELLOW'

        def to_dict(self):
            return {
                'workbook_name': self.workbook_name,
                'timestamp': '2025-01-15T10:30:00Z',
                'overall_score': self.overall_score,
                'totals': {
                    'checks': self.total_checks,
                    'pass': self.total_pass,
                    'warn': self.total_warn,
                    'fail': self.total_fail,
                },
                'categories': [
                    {
                        'name': 'Test Category',
                        'worst_severity': 'warn',
                        'total_checks': 3,
                        'total_pass': 2,
                        'total_warn': 1,
                        'total_fail': 0,
                        'checks': [
                            {
                                'name': 'Check A',
                                'severity': 'pass',
                                'detail': 'All good',
                                'recommendation': '',
                            },
                            {
                                'name': 'Check B',
                                'severity': 'warn',
                                'detail': 'Needs review',
                                'recommendation': 'Review the formula',
                            },
                        ],
                    },
                ],
            }

    def test_generate_report_package_creates_zip(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            result = generate_report_package(
                self._MockReport(), "<html><body>Test</body></html>", path
            )
            self.assertTrue(os.path.exists(result))
            self.assertTrue(result.endswith('.zip'))

    def test_package_contains_all_files(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport(), "<html><body>Test</body></html>", path
            )
            with zipfile.ZipFile(path, 'r') as zf:
                names = zf.namelist()
                self.assertIn('assessment_report.html', names)
                self.assertIn('assessment_report.pdf.html', names)
                self.assertIn('executive_summary.pptx', names)
                self.assertIn('assessment_data.json', names)
                self.assertIn('fidelity_checks.csv', names)
                self.assertIn('README.txt', names)

    def test_package_html_content(self):
        from powerbi_import.report_packager import generate_report_package

        html = "<html><body><h1>My Report</h1></body></html>"
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(self._MockReport(), html, path)
            with zipfile.ZipFile(path, 'r') as zf:
                content = zf.read('assessment_report.html').decode('utf-8')
                self.assertIn('My Report', content)

    def test_package_pdf_html_has_print_css(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport(), "<html><body>Test</body></html>", path
            )
            with zipfile.ZipFile(path, 'r') as zf:
                pdf_html = zf.read('assessment_report.pdf.html').decode('utf-8')
                self.assertIn('@media print', pdf_html)

    def test_package_json_is_valid(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport(), "<html><body>Test</body></html>", path
            )
            with zipfile.ZipFile(path, 'r') as zf:
                data = json.loads(zf.read('assessment_data.json'))
                self.assertEqual(data['workbook_name'], 'TestWorkbook')
                self.assertEqual(data['overall_score'], 'YELLOW')

    def test_package_csv_has_headers(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport(), "<html><body>Test</body></html>", path
            )
            with zipfile.ZipFile(path, 'r') as zf:
                csv_content = zf.read('fidelity_checks.csv').decode('utf-8')
                self.assertIn('Category', csv_content)
                self.assertIn('Severity', csv_content)
                self.assertIn('Check A', csv_content)

    def test_package_readme_has_workbook_name(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport('Sales Dashboard'), "<html></html>", path
            )
            with zipfile.ZipFile(path, 'r') as zf:
                readme = zf.read('README.txt').decode('utf-8')
                self.assertIn('Sales Dashboard', readme)
                self.assertIn('YELLOW', readme)

    def test_package_pptx_is_valid(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport(), "<html><body>Test</body></html>", path
            )
            with zipfile.ZipFile(path, 'r') as zf:
                pptx_bytes = zf.read('executive_summary.pptx')
                # Verify the PPTX is a valid ZIP
                import io
                pptx_zf = zipfile.ZipFile(io.BytesIO(pptx_bytes))
                self.assertIn('[Content_Types].xml', pptx_zf.namelist())
                pptx_zf.close()

    def test_package_with_migration_stats(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "package.zip")
            generate_report_package(
                self._MockReport(), "<html></html>", path,
                migration_stats={'visuals': 5},
            )
            self.assertTrue(os.path.exists(path))

    def test_package_creates_parent_dirs(self):
        from powerbi_import.report_packager import generate_report_package

        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "sub", "dir", "package.zip")
            generate_report_package(
                self._MockReport(), "<html></html>", path
            )
            self.assertTrue(os.path.exists(path))


class TestBuildChecksCsv(unittest.TestCase):
    """Tests for _build_checks_csv helper."""

    def test_empty_categories(self):
        from powerbi_import.report_packager import _build_checks_csv

        result = _build_checks_csv({'categories': []})
        lines = result.strip().split('\n')
        self.assertEqual(len(lines), 1)  # header only

    def test_multiple_checks(self):
        from powerbi_import.report_packager import _build_checks_csv

        data = {
            'categories': [
                {
                    'name': 'Cat1',
                    'checks': [
                        {'name': 'C1', 'severity': 'pass', 'detail': 'OK', 'recommendation': ''},
                        {'name': 'C2', 'severity': 'warn', 'detail': 'Issue', 'recommendation': 'Fix it'},
                    ],
                },
            ],
        }
        result = _build_checks_csv(data)
        lines = result.strip().split('\n')
        self.assertEqual(len(lines), 3)  # header + 2 rows


class TestBuildReadme(unittest.TestCase):
    """Tests for _build_readme helper."""

    def test_readme_content(self):
        from powerbi_import.report_packager import _build_readme

        data = {
            'overall_score': 'GREEN',
            'timestamp': '2025-01-15T10:30:00Z',
            'totals': {'checks': 10, 'pass': 9, 'warn': 1, 'fail': 0},
        }
        result = _build_readme('My Workbook', data)
        self.assertIn('My Workbook', result)
        self.assertIn('GREEN', result)
        self.assertIn('10 total', result)
        self.assertIn('executive_summary.pptx', result)


if __name__ == '__main__':
    unittest.main()
