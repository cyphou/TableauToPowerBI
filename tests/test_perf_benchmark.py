"""Sprint 132.2 — E2E performance benchmark for large workbooks.

Success criteria (from ROADMAP.md Sprint 132):
    - Extraction of 500-measure workbook < 60 s
    - Generation < 120 s
    - Peak RSS < 2 GB  (tracked via tracemalloc)
    - Full pipeline < 180 s

The wall-clock limits are only enforced on an uninstrumented interpreter. Under
coverage's tracer the 3.12 CI jobs needed 220-344 s for the generation and
pipeline runs (limits: 120 s and 180 s), so CI runs this module a second time,
without coverage, where the limits are enforced.

Run:
    py -m pytest tests/test_perf_benchmark.py -v --tb=short
"""

import os
import shutil
import sys
import tempfile
import time
import tracemalloc
import unittest
from unittest import mock

# Project root on path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tableau_export'))
sys.path.insert(0, os.path.join(ROOT, 'powerbi_import'))

from tests.large_workbook_generator import generate_large_twb

# ── Thresholds ───────────────────────────────────────────────────────

THRESHOLD_EXTRACTION_SECONDS = 60
THRESHOLD_GENERATION_SECONDS = 120
THRESHOLD_PIPELINE_SECONDS = 180
THRESHOLD_PEAK_MEMORY_MB = 2048  # 2 GB

# Fixture parameters (matches roadmap: 500 measures, 100 worksheets, 50 datasources)
FIXTURE_PARAMS = dict(
    num_measures=500,
    num_worksheets=100,
    num_datasources=50,
    num_dashboards=20,
    num_parameters=30,
    num_sets=20,
    num_groups=15,
    num_bins=10,
    num_hierarchies=15,
    seed=42,
)


def _timing_is_instrumented():
    """True while a tracer, profiler or monitoring tool (coverage, debugger) is active."""
    if sys.gettrace() is not None or sys.getprofile() is not None:
        return True
    monitoring = getattr(sys, 'monitoring', None)
    return monitoring is not None and any(
        monitoring.get_tool(tool_id) is not None for tool_id in range(6))


def _assert_within_limit(test, elapsed, limit, label):
    if _timing_is_instrumented():
        print(f'\n  {label}: {elapsed:.2f}s (limit of {limit}s not enforced '
              f'while a tracer or coverage is active)')
        return
    test.assertLess(
        elapsed, limit, f'{label} took {elapsed:.2f}s (limit: {limit}s)')


class TestLargeWorkbookPerformance(unittest.TestCase):
    """Performance benchmarks for large-workbook extraction and generation."""

    @classmethod
    def setUpClass(cls):
        """Generate the synthetic TWB fixture once for all tests."""
        cls._tmp_dir = tempfile.mkdtemp(prefix='perf_bench_')
        cls._twb_path = os.path.join(cls._tmp_dir, 'large_500.twb')
        generate_large_twb(cls._twb_path, **FIXTURE_PARAMS)
        cls._output_dir = os.path.join(cls._tmp_dir, 'output')
        os.makedirs(cls._output_dir, exist_ok=True)
        # Extraction output dir (where JSON files go)
        cls._extract_dir = os.path.join(cls._tmp_dir, 'tableau_export')
        os.makedirs(cls._extract_dir, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    # ── Extraction benchmark ─────────────────────────────────────

    def test_extraction_under_threshold(self):
        """Extraction of 500-measure workbook must complete in < 60s."""
        from extract_tableau_data import TableauExtractor

        tracemalloc.start()
        t0 = time.perf_counter()

        extractor = TableauExtractor(self._twb_path, output_dir=self._extract_dir)
        result = extractor.extract_all()

        elapsed = time.perf_counter() - t0
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak_bytes / (1024 * 1024)
        print(f'\n  Extraction: {elapsed:.2f}s | peak RSS: {peak_mb:.1f} MB')

        self.assertTrue(result, 'Extraction returned False (failure)')
        _assert_within_limit(
            self, elapsed, THRESHOLD_EXTRACTION_SECONDS, 'Extraction')
        self.assertLess(
            peak_mb, THRESHOLD_PEAK_MEMORY_MB,
            f'Extraction peak memory {peak_mb:.1f} MB (limit: {THRESHOLD_PEAK_MEMORY_MB} MB)',
        )

    # ── Generation benchmark ─────────────────────────────────────

    def test_generation_under_threshold(self):
        """Generation from large extraction must complete in < 120s."""
        from extract_tableau_data import TableauExtractor
        from import_to_powerbi import PowerBIImporter

        # Ensure extraction data exists
        extractor = TableauExtractor(self._twb_path, output_dir=self._extract_dir)
        extractor.extract_all()
        extractor.save_extractions()

        tracemalloc.start()
        t0 = time.perf_counter()

        importer = PowerBIImporter(source_dir=self._extract_dir)
        importer.import_all(
            generate_pbip=True,
            report_name='PerfBench',
            output_dir=self._output_dir,
        )

        elapsed = time.perf_counter() - t0
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak_bytes / (1024 * 1024)
        print(f'\n  Generation: {elapsed:.2f}s | peak RSS: {peak_mb:.1f} MB')

        _assert_within_limit(
            self, elapsed, THRESHOLD_GENERATION_SECONDS, 'Generation')
        self.assertLess(
            peak_mb, THRESHOLD_PEAK_MEMORY_MB,
            f'Generation peak memory {peak_mb:.1f} MB (limit: {THRESHOLD_PEAK_MEMORY_MB} MB)',
        )

    # ── Full pipeline benchmark ──────────────────────────────────

    def test_full_pipeline_under_threshold(self):
        """Full extraction + generation must complete in < 180s."""
        from extract_tableau_data import TableauExtractor
        from import_to_powerbi import PowerBIImporter

        pipeline_dir = os.path.join(self._tmp_dir, 'pipeline_run')
        extract_out = os.path.join(pipeline_dir, 'tableau_export')
        gen_out = os.path.join(pipeline_dir, 'output')
        os.makedirs(extract_out, exist_ok=True)
        os.makedirs(gen_out, exist_ok=True)

        tracemalloc.start()
        t0 = time.perf_counter()

        # Step 1: Extract
        extractor = TableauExtractor(self._twb_path, output_dir=extract_out)
        result = extractor.extract_all()
        extractor.save_extractions()

        # Step 2: Generate
        importer = PowerBIImporter(source_dir=extract_out)
        importer.import_all(
            generate_pbip=True,
            report_name='PerfPipeline',
            output_dir=gen_out,
        )

        elapsed = time.perf_counter() - t0
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        peak_mb = peak_bytes / (1024 * 1024)
        print(f'\n  Full pipeline: {elapsed:.2f}s | peak RSS: {peak_mb:.1f} MB')

        self.assertTrue(result)
        _assert_within_limit(
            self, elapsed, THRESHOLD_PIPELINE_SECONDS, 'Full pipeline')
        self.assertLess(
            peak_mb, THRESHOLD_PEAK_MEMORY_MB,
            f'Pipeline peak memory {peak_mb:.1f} MB (limit: {THRESHOLD_PEAK_MEMORY_MB} MB)',
        )


class TestTimingGuard(unittest.TestCase):
    """The wall-clock limits are skipped only while something instruments the run."""

    @staticmethod
    def _instrumented(trace=None, profile=None, monitoring_tools=None):
        monitoring = mock.Mock()
        monitoring.get_tool.side_effect = (monitoring_tools or {}).get
        with mock.patch.object(sys, 'gettrace', return_value=trace), \
                mock.patch.object(sys, 'getprofile', return_value=profile), \
                mock.patch.object(sys, 'monitoring', monitoring, create=True):
            return _timing_is_instrumented()

    def test_clean_interpreter_is_not_instrumented(self):
        self.assertFalse(self._instrumented())

    def test_tracer_profiler_and_monitoring_tool_are_detected(self):
        self.assertTrue(self._instrumented(trace=object()))
        self.assertTrue(self._instrumented(profile=object()))
        self.assertTrue(self._instrumented(monitoring_tools={1: 'coverage.py'}))

    def test_limit_is_enforced_without_instrumentation(self):
        with mock.patch(f'{__name__}._timing_is_instrumented', return_value=False):
            with self.assertRaises(AssertionError):
                _assert_within_limit(self, 5.0, 1, 'Slow step')
            _assert_within_limit(self, 0.5, 1, 'Fast step')

    def test_limit_is_not_enforced_under_instrumentation(self):
        with mock.patch(f'{__name__}._timing_is_instrumented', return_value=True):
            _assert_within_limit(self, 500.0, 1, 'Slow step')


class TestMemoryCeiling(unittest.TestCase):
    """Sprint 132.5 — Memory ceiling guards.

    Assert no single migration operation exceeds 500 MB of traced memory.
    """

    MEMORY_CEILING_MB = 500

    @classmethod
    def setUpClass(cls):
        cls._tmp_dir = tempfile.mkdtemp(prefix='mem_ceil_')
        cls._twb_path = os.path.join(cls._tmp_dir, 'medium_200.twb')
        generate_large_twb(
            cls._twb_path,
            num_measures=200,
            num_worksheets=40,
            num_datasources=20,
            seed=99,
        )
        cls._extract_dir = os.path.join(cls._tmp_dir, 'tableau_export')
        os.makedirs(cls._extract_dir, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmp_dir, ignore_errors=True)

    def _measure_peak(self, func):
        """Run func and return (result, peak_mb)."""
        tracemalloc.start()
        result = func()
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return result, peak_bytes / (1024 * 1024)

    def test_extraction_memory_ceiling(self):
        """Extraction must stay under 500 MB."""
        from extract_tableau_data import TableauExtractor

        def do_extract():
            ext = TableauExtractor(self._twb_path, output_dir=self._extract_dir)
            ext.extract_all()
            ext.save_extractions()
            return True

        _, peak_mb = self._measure_peak(do_extract)
        print(f'\n  Extraction peak: {peak_mb:.1f} MB (ceiling: {self.MEMORY_CEILING_MB} MB)')
        self.assertLess(peak_mb, self.MEMORY_CEILING_MB)

    def test_generation_memory_ceiling(self):
        """Generation must stay under 500 MB."""
        from extract_tableau_data import TableauExtractor
        from import_to_powerbi import PowerBIImporter

        # Pre-extract
        ext = TableauExtractor(self._twb_path, output_dir=self._extract_dir)
        ext.extract_all()
        ext.save_extractions()

        gen_out = os.path.join(self._tmp_dir, 'gen_out')
        os.makedirs(gen_out, exist_ok=True)

        def do_generate():
            imp = PowerBIImporter(source_dir=self._extract_dir)
            imp.import_all(generate_pbip=True, report_name='MemCeil', output_dir=gen_out)
            return True

        _, peak_mb = self._measure_peak(do_generate)
        print(f'\n  Generation peak: {peak_mb:.1f} MB (ceiling: {self.MEMORY_CEILING_MB} MB)')
        self.assertLess(peak_mb, self.MEMORY_CEILING_MB)

    def test_dax_converter_memory_ceiling(self):
        """Converting 1000 formulas must stay under 500 MB."""
        from dax_converter import convert_tableau_formula_to_dax

        formulas = [
            f'SUM([Sales_{i}])' for i in range(500)
        ] + [
            f'IF ISNULL([Col_{i}]) THEN 0 ELSE [Col_{i}] END' for i in range(500)
        ]

        def do_convert():
            results = []
            for f in formulas:
                results.append(convert_tableau_formula_to_dax(f))
            return results

        _, peak_mb = self._measure_peak(do_convert)
        print(f'\n  DAX converter peak: {peak_mb:.1f} MB (ceiling: {self.MEMORY_CEILING_MB} MB)')
        self.assertLess(peak_mb, self.MEMORY_CEILING_MB)

    def test_m_query_builder_memory_ceiling(self):
        """Building 200 M queries must stay under 500 MB."""
        from m_query_builder import generate_power_query_m

        connections = [
            {'type': 'sqlserver', 'details': {'server': 'localhost', 'dbname': 'TestDB'}}
            for _ in range(200)
        ]
        tables = [
            {'name': f'Table_{i}',
             'columns': [{'name': f'Col_{j}', 'datatype': 'string'} for j in range(20)]}
            for i in range(200)
        ]

        def do_build():
            results = []
            for conn, tbl in zip(connections, tables):
                results.append(generate_power_query_m(conn, tbl))
            return results

        _, peak_mb = self._measure_peak(do_build)
        print(f'\n  M query builder peak: {peak_mb:.1f} MB (ceiling: {self.MEMORY_CEILING_MB} MB)')
        self.assertLess(peak_mb, self.MEMORY_CEILING_MB)


if __name__ == '__main__':
    unittest.main()
