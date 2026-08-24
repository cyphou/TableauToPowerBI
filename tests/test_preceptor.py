"""Tests for the preceptorship loop engine (powerbi_import/preceptor.py)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from powerbi_import.preceptor import (
    CoachingItem,
    PreceptorLoop,
    ReviewCycle,
    ReviewReport,
    ReviewScorecard,
    _load_extraction_data,
    _review_completeness,
    _review_dax_correctness,
    _review_m_query_validity,
    _review_pbir_fidelity,
    _review_tmdl_structure,
    _review_visual_equivalence,
    run_preceptor_review,
    DIMENSIONS,
    MAX_CYCLES,
    MIN_PASS_SCORE,
    SSIM_THRESHOLD,
)


# ── Helpers ──────────────────────────────────────────────────────

def _make_pbip_project(tmp, name='TestReport', *,
                       tmdl_content='', m_content='',
                       report_json=None, visuals=None,
                       model_tmdl=None, pages=None,
                       definition_pbir=True):
    """Create a minimal .pbip directory structure for testing."""
    proj = Path(tmp) / name
    sm_dir = proj / f'{name}.SemanticModel' / 'definition' / 'tables'
    report_dir = proj / f'{name}.Report' / 'definition'

    sm_dir.mkdir(parents=True)
    report_dir.mkdir(parents=True)

    # model.tmdl
    model_path = proj / f'{name}.SemanticModel' / 'definition' / 'model.tmdl'
    model_path.write_text(model_tmdl or 'model Model\n', encoding='utf-8')

    # Table TMDL
    if tmdl_content:
        (sm_dir / 'Sales.tmdl').write_text(tmdl_content, encoding='utf-8')

    # M partition content in a separate table
    if m_content:
        (sm_dir / 'MTable.tmdl').write_text(m_content, encoding='utf-8')

    # report.json
    rj = report_json or {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/report/2.0.0/schema.json'}
    (report_dir / 'report.json').write_text(json.dumps(rj), encoding='utf-8')

    # definition.pbir
    if definition_pbir:
        pbir = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json'}
        (report_dir / 'definition.pbir').write_text(json.dumps(pbir), encoding='utf-8')

    # Pages/visuals
    if pages:
        for page_name, visual_list in pages.items():
            page_dir = report_dir / 'pages' / page_name / 'visuals'
            page_dir.mkdir(parents=True)
            for i, v_json in enumerate(visual_list):
                v_dir = page_dir / f'visual_{i}'
                v_dir.mkdir()
                (v_dir / 'visual.json').write_text(json.dumps(v_json), encoding='utf-8')

    # .pbip file
    (proj / f'{name}.pbip').write_text('{}', encoding='utf-8')

    return str(proj)


def _make_extraction(tmp, **overrides):
    """Create extraction JSON files and return the directory path."""
    ext_dir = Path(tmp) / 'extraction'
    ext_dir.mkdir(exist_ok=True)

    defaults = {
        'worksheets': [{'name': 'Sheet1', 'mark_type': 'bar'}],
        'dashboards': [{'name': 'Dashboard1'}],
        'datasources': [{'name': 'ds1', 'tables': [
            {'name': 'Sales', 'columns': [
                {'name': 'Amount', 'datatype': 'real'},
            ]}
        ], 'relationships': []}],
        'calculations': [{'name': 'Profit', 'formula': 'SUM([Sales]) - SUM([Cost])'}],
        'parameters': [],
        'filters': [],
        'stories': [],
        'actions': [],
        'sets': [],
        'groups': [],
        'bins': [],
        'hierarchies': [],
        'sort_orders': [],
        'aliases': [],
        'custom_sql': [],
        'user_filters': [],
        'hyper_files': [],
    }
    defaults.update(overrides)

    for name, data in defaults.items():
        (ext_dir / f'{name}.json').write_text(json.dumps(data), encoding='utf-8')

    return str(ext_dir)


# ── Test Classes ─────────────────────────────────────────────────

class TestCoachingItem(unittest.TestCase):
    def test_to_dict_minimal(self):
        item = CoachingItem('completeness', 3, 'Missing visuals')
        d = item.to_dict()
        self.assertEqual(d['dimension'], 'completeness')
        self.assertEqual(d['score'], 3)
        self.assertEqual(d['issue'], 'Missing visuals')
        self.assertNotIn('location', d)

    def test_to_dict_full(self):
        item = CoachingItem(
            'dax_correctness', 2, 'Leak found',
            location='Sales.tmdl', fix='Replace COUNTD',
            example_before='COUNTD()', example_after='DISTINCTCOUNT()',
        )
        d = item.to_dict()
        self.assertEqual(d['location'], 'Sales.tmdl')
        self.assertEqual(d['fix'], 'Replace COUNTD')
        self.assertEqual(d['example_before'], 'COUNTD()')
        self.assertEqual(d['example_after'], 'DISTINCTCOUNT()')


class TestReviewScorecard(unittest.TestCase):
    def test_empty_scorecard(self):
        sc = ReviewScorecard()
        self.assertEqual(sc.average(), 0.0)
        self.assertFalse(sc.passed())

    def test_passing_scorecard(self):
        sc = ReviewScorecard()
        for dim in DIMENSIONS:
            sc.set_score(dim, 5)
        self.assertEqual(sc.average(), 5.0)
        self.assertTrue(sc.passed())

    def test_failing_scorecard(self):
        sc = ReviewScorecard()
        for dim in DIMENSIONS:
            sc.set_score(dim, 2)
        self.assertEqual(sc.average(), 2.0)
        self.assertFalse(sc.passed())

    def test_borderline_scorecard(self):
        sc = ReviewScorecard()
        for dim in DIMENSIONS:
            sc.set_score(dim, 4)
        self.assertEqual(sc.average(), 4.0)
        self.assertTrue(sc.passed())

    def test_score_clamped(self):
        sc = ReviewScorecard()
        sc.set_score('completeness', 10)
        self.assertEqual(sc.scores['completeness'], 5)
        sc.set_score('completeness', -3)
        self.assertEqual(sc.scores['completeness'], 1)

    def test_to_dict(self):
        sc = ReviewScorecard()
        sc.set_score('completeness', 4, 'All good')
        d = sc.to_dict()
        self.assertIn('scores', d)
        self.assertIn('details', d)
        self.assertIn('average', d)
        self.assertIn('passed', d)
        self.assertEqual(d['details']['completeness'], 'All good')


class TestReviewCycle(unittest.TestCase):
    def test_cycle_to_dict(self):
        cycle = ReviewCycle(1)
        cycle.scorecard.set_score('completeness', 5)
        cycle.add_coaching(CoachingItem('completeness', 5, 'OK'))
        d = cycle.to_dict()
        self.assertEqual(d['cycle'], 1)
        self.assertIn('timestamp', d)
        self.assertEqual(len(d['coaching_items']), 1)


class TestReviewReport(unittest.TestCase):
    def test_empty_report(self):
        report = ReviewReport('TestReport')
        self.assertEqual(report.total_cycles, 0)
        self.assertEqual(report.status, ReviewReport.COACHING)

    def test_report_with_cycles(self):
        report = ReviewReport('TestReport')
        cycle = ReviewCycle(1)
        for dim in DIMENSIONS:
            cycle.scorecard.set_score(dim, 5)
        report.add_cycle(cycle)
        report.status = ReviewReport.APPROVED
        self.assertEqual(report.total_cycles, 1)
        self.assertTrue(report.final_scorecard.passed())

    def test_to_dict(self):
        report = ReviewReport('TestReport')
        d = report.to_dict()
        self.assertEqual(d['report_name'], 'TestReport')
        self.assertIn('created_at', d)
        self.assertEqual(d['total_cycles'], 0)

    def test_to_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = ReviewReport('TestReport')
            out = os.path.join(tmp, 'review.json')
            report.to_json(out)
            self.assertTrue(os.path.exists(out))
            with open(out, 'r', encoding='utf-8') as f:
                data = json.load(f)
            self.assertEqual(data['report_name'], 'TestReport')

    def test_to_console(self):
        """Ensure to_console runs without crashing."""
        report = ReviewReport('TestReport')
        cycle = ReviewCycle(1)
        for dim in DIMENSIONS:
            cycle.scorecard.set_score(dim, 3)
        cycle.add_coaching(CoachingItem('completeness', 3, 'Missing items'))
        report.add_cycle(cycle)
        report.to_console()  # Should not raise


class TestLoadExtractionData(unittest.TestCase):
    def test_load_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_dir = _make_extraction(tmp)
            data = _load_extraction_data(ext_dir)
            self.assertEqual(len(data['worksheets']), 1)
            self.assertEqual(len(data['calculations']), 1)

    def test_missing_files_return_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = _load_extraction_data(tmp)
            for key in data:
                self.assertEqual(data[key], [])


class TestReviewCompleteness(unittest.TestCase):
    def test_perfect_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(
                tmp,
                tmdl_content='table Sales\n  measure Profit = 1\n  column Amount\n',
                pages={'page1': [visual_json]},
            )
            ext_dir = _make_extraction(tmp)
            data = _load_extraction_data(ext_dir)

            score, detail, coaching = _review_completeness(pbip, data)
            self.assertGreaterEqual(score, 4)
            self.assertEqual(len(coaching), 0)

    def test_missing_visuals(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            ext_dir = _make_extraction(tmp, worksheets=[
                {'name': 'S1'}, {'name': 'S2'}, {'name': 'S3'},
            ])
            data = _load_extraction_data(ext_dir)

            score, detail, coaching = _review_completeness(pbip, data)
            self.assertLess(score, 5)
            self.assertTrue(any('visual' in c.issue.lower() for c in coaching))


class TestReviewDaxCorrectness(unittest.TestCase):
    def test_clean_dax(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = (
                'table Sales\n'
                '  measure Profit\n'
                '    expression = SUM(Sales[Amount]) - SUM(Sales[Cost])\n'
            )
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, detail, coaching = _review_dax_correctness(pbip, {})
            self.assertEqual(score, 5)
            self.assertEqual(len(coaching), 0)

    def test_tableau_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = (
                'table Sales\n'
                '  measure UniqueCustomers\n'
                '    expression = COUNTD(Sales[CustomerID])\n'
            )
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, detail, coaching = _review_dax_correctness(pbip, {})
            self.assertLess(score, 5)
            self.assertTrue(any('leak' in c.issue.lower() or 'COUNTD' in c.issue for c in coaching))

    def test_unbalanced_parens(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = (
                'table Sales\n'
                '  measure Bad\n'
                '    expression = SUM(Sales[Amount]\n'
            )
            pbip = _make_pbip_project(tmp, tmdl_content=tmdl)
            score, detail, coaching = _review_dax_correctness(pbip, {})
            self.assertLess(score, 5)
            self.assertTrue(any('paren' in c.issue.lower() for c in coaching))


class TestReviewMQueryValidity(unittest.TestCase):
    def test_valid_m_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = (
                'table MTable\n'
                '  partition p = m\n'
                '    mode: import\n'
                '    expression =\n'
                '      ```\n'
                '      let\n'
                '        Source = Sql.Database("server", "db"),\n'
                '        Result = if true then 1 else 0\n'
                '      in\n'
                '        Result\n'
                '      ```\n'
            )
            pbip = _make_pbip_project(tmp, m_content=m)
            score, detail, coaching = _review_m_query_validity(pbip, {})
            self.assertEqual(score, 5)

    def test_unbalanced_if_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = (
                'table MTable\n'
                '  partition p = m\n'
                '    expression =\n'
                '      ```\n'
                '      let\n'
                '        Result = if true then 1\n'
                '      in\n'
                '        Result\n'
                '      ```\n'
            )
            pbip = _make_pbip_project(tmp, m_content=m)
            score, detail, coaching = _review_m_query_validity(pbip, {})
            self.assertLess(score, 5)
            self.assertTrue(any('if' in c.issue.lower() and 'else' in c.issue.lower() for c in coaching))


class TestReviewTmdlStructure(unittest.TestCase):
    def test_valid_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, tmdl_content='table Sales\n  column Amount\n')
            score, detail, coaching = _review_tmdl_structure(pbip, {
                'datasources': [{'tables': [], 'relationships': []}],
                'user_filters': [],
            })
            self.assertGreaterEqual(score, 4)

    def test_missing_model_tmdl(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp, model_tmdl='')
            # Remove model.tmdl
            model_path = Path(pbip) / 'TestReport.SemanticModel' / 'definition' / 'model.tmdl'
            model_path.unlink()

            score, detail, coaching = _review_tmdl_structure(pbip, {
                'datasources': [], 'user_filters': [],
            })
            self.assertLess(score, 3)
            self.assertTrue(any('model.tmdl' in c.issue for c in coaching))

    def test_missing_rls_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            score, detail, coaching = _review_tmdl_structure(pbip, {
                'datasources': [],
                'user_filters': [{'field': 'Region', 'users': ['alice']}],
            })
            self.assertLess(score, 5)
            self.assertTrue(any('RLS' in c.issue or 'user filter' in c.issue.lower() for c in coaching))


class TestReviewPbirFidelity(unittest.TestCase):
    def test_valid_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(tmp, pages={'page1': [visual_json]})
            score, detail, coaching = _review_pbir_fidelity(pbip, {
                'dashboards': [{'name': 'D1'}], 'filters': [],
            })
            self.assertGreaterEqual(score, 4)

    def test_missing_report_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            # Remove report.json
            rj = Path(pbip) / 'TestReport.Report' / 'definition' / 'report.json'
            rj.unlink()

            score, detail, coaching = _review_pbir_fidelity(pbip, {
                'dashboards': [], 'filters': [],
            })
            self.assertLess(score, 3)
            self.assertTrue(any('report.json' in c.issue for c in coaching))

    def test_invalid_visual_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Visual missing $schema
            bad_visual = {'name': 'v1'}
            pbip = _make_pbip_project(tmp, pages={'page1': [bad_visual]})
            score, detail, coaching = _review_pbir_fidelity(pbip, {
                'dashboards': [{'name': 'D1'}], 'filters': [],
            })
            self.assertLess(score, 5)
            self.assertTrue(any('invalid' in c.issue.lower() for c in coaching))


class TestPreceptorLoop(unittest.TestCase):
    def test_single_review_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(
                tmp,
                tmdl_content='table Sales\n  measure Profit = 1\n  column Amount\n',
                pages={'page1': [visual_json]},
            )
            ext_dir = _make_extraction(tmp)

            loop = PreceptorLoop()
            data = _load_extraction_data(ext_dir)
            cycle = loop.review(pbip, data)
            self.assertIsInstance(cycle, ReviewCycle)
            self.assertEqual(cycle.cycle_number, 1)
            # Score should be populated for all dimensions
            self.assertEqual(len(cycle.scorecard.scores), len(DIMENSIONS))

    def test_full_loop_approves_clean_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(
                tmp,
                tmdl_content='table Sales\n  measure Profit = 1\n  column Amount\n',
                pages={'page1': [visual_json]},
            )
            ext_dir = _make_extraction(tmp)

            loop = PreceptorLoop()
            report = loop.run(pbip, ext_dir)
            self.assertEqual(report.status, ReviewReport.APPROVED)
            self.assertEqual(report.total_cycles, 1)

    def test_full_loop_escalates_bad_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Project with Tableau function leaks + missing visuals + no pages
            tmdl = (
                'table Sales\n'
                '  measure Bad1\n'
                '    expression = COUNTD(Sales[ID])\n'
                '  measure Bad2\n'
                '    expression = ZN(Sales[Amount])\n'
                '  measure Bad3\n'
                '    expression = IFNULL(Sales[X], 0)\n'
            )
            pbip = _make_pbip_project(
                tmp, tmdl_content=tmdl, definition_pbir=False,
            )
            # Remove report.json to also fail PBIR fidelity
            rj = Path(pbip) / 'TestReport.Report' / 'definition' / 'report.json'
            rj.unlink()
            # Remove model.tmdl to fail TMDL structure
            model_path = Path(pbip) / 'TestReport.SemanticModel' / 'definition' / 'model.tmdl'
            model_path.unlink()

            ext_dir = _make_extraction(tmp, worksheets=[
                {'name': f'S{i}'} for i in range(5)
            ], user_filters=[{'field': 'Region', 'users': ['alice']}])

            loop = PreceptorLoop(max_cycles=2)
            report = loop.run(pbip, ext_dir, on_escalate='block')
            self.assertEqual(report.status, ReviewReport.ESCALATED_BLOCK)
            self.assertEqual(report.total_cycles, 2)
            self.assertIn('Manual intervention', report.escalation_reason)

    def test_escalate_warn_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmdl = 'table Sales\n  measure Bad = COUNTD(Sales[ID])\n'
            pbip = _make_pbip_project(
                tmp, tmdl_content=tmdl, definition_pbir=False,
            )
            # Remove report.json + model.tmdl to fail multiple dimensions
            rj = Path(pbip) / 'TestReport.Report' / 'definition' / 'report.json'
            rj.unlink()
            model_path = Path(pbip) / 'TestReport.SemanticModel' / 'definition' / 'model.tmdl'
            model_path.unlink()

            ext_dir = _make_extraction(tmp, worksheets=[
                {'name': f'S{i}'} for i in range(5)
            ], user_filters=[{'field': 'X', 'users': ['bob']}])

            loop = PreceptorLoop(max_cycles=1)
            report = loop.run(pbip, ext_dir, on_escalate='warn')
            self.assertEqual(report.status, ReviewReport.ESCALATED_WARN)
            self.assertIn('quality warnings', report.escalation_reason)

    def test_extraction_data_as_dict(self):
        """run() accepts extraction data as a dict instead of a dir path."""
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(
                tmp,
                tmdl_content='table Sales\n  measure P = 1\n  column A\n',
                pages={'page1': [visual_json]},
            )
            data = {
                'worksheets': [{'name': 'Sheet1'}],
                'dashboards': [{'name': 'D1'}],
                'datasources': [{'tables': [{'name': 'Sales', 'columns': []}], 'relationships': []}],
                'calculations': [{'name': 'P'}],
                'parameters': [],
                'filters': [],
                'user_filters': [],
            }

            loop = PreceptorLoop()
            report = loop.run(pbip, data)
            self.assertIn(report.status, (ReviewReport.APPROVED, ReviewReport.COACHING))


class TestRunPreceptorReview(unittest.TestCase):
    def test_convenience_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(
                tmp,
                tmdl_content='table Sales\n  measure P = 1\n  column A\n',
                pages={'page1': [visual_json]},
            )
            ext_dir = _make_extraction(tmp)
            out = os.path.join(tmp, 'review.json')

            report = run_preceptor_review(
                pbip, ext_dir, output_path=out, quiet=True,
            )
            self.assertIsInstance(report, ReviewReport)
            self.assertTrue(os.path.exists(out))


class TestConstants(unittest.TestCase):
    def test_dimensions_count(self):
        self.assertEqual(len(DIMENSIONS), 6)

    def test_min_pass_score(self):
        self.assertEqual(MIN_PASS_SCORE, 4.0)

    def test_max_cycles(self):
        self.assertEqual(MAX_CYCLES, 3)

    def test_ssim_threshold(self):
        self.assertEqual(SSIM_THRESHOLD, 0.85)


class TestReviewVisualEquivalence(unittest.TestCase):
    def test_no_screenshots_returns_full_score(self):
        """When no screenshots dir exists, dimension is skipped with 5★."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            score, detail, coaching = _review_visual_equivalence(pbip, {})
            self.assertEqual(score, 5)
            self.assertIn('skipped', detail)
            self.assertEqual(len(coaching), 0)

    def test_empty_source_dir_returns_full_score(self):
        """When screenshots/source/ exists but is empty, skip."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            ss_dir = Path(tmp) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)
            score, detail, coaching = _review_visual_equivalence(
                pbip, {}, screenshots_dir=str(ss_dir))
            self.assertEqual(score, 5)
            self.assertIn('skipped', detail)

    def test_identical_screenshots_pass(self):
        """Identical images should score 5★."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            ss_dir = Path(tmp) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)

            # Create identical fake PNG files
            fake_png = b'\x89PNG\r\n\x1a\n' + b'\x00' * 200
            (ss_dir / 'source' / 'sheet1.png').write_bytes(fake_png)
            (ss_dir / 'output' / 'sheet1.png').write_bytes(fake_png)

            score, detail, coaching = _review_visual_equivalence(
                pbip, {}, screenshots_dir=str(ss_dir))
            self.assertEqual(score, 5)
            self.assertEqual(len(coaching), 0)

    def test_different_screenshots_fail(self):
        """Very different images should score below threshold."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            ss_dir = Path(tmp) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)

            # Create completely different fake PNG data
            src_data = b'\x89PNG\r\n\x1a\n' + b'\xff' * 500
            out_data = b'\x89PNG\r\n\x1a\n' + b'\x00' * 300
            (ss_dir / 'source' / 'chart1.png').write_bytes(src_data)
            (ss_dir / 'output' / 'chart1.png').write_bytes(out_data)

            score, detail, coaching = _review_visual_equivalence(
                pbip, {}, screenshots_dir=str(ss_dir))
            self.assertLess(score, 5)
            self.assertTrue(len(coaching) > 0)
            self.assertIn('SSIM', coaching[0].issue)

    def test_missing_output_screenshot(self):
        """Source screenshot without matching output should penalize."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            ss_dir = Path(tmp) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)

            # Source exists but no matching output
            fake_png = b'\x89PNG\r\n\x1a\n' + b'\x42' * 200
            (ss_dir / 'source' / 'visual1.png').write_bytes(fake_png)

            score, detail, coaching = _review_visual_equivalence(
                pbip, {}, screenshots_dir=str(ss_dir))
            self.assertLess(score, 5)
            self.assertTrue(any('No output screenshot' in c.issue for c in coaching))

    def test_auto_discovers_screenshots_beside_pbip(self):
        """Screenshots dir auto-discovered next to pbip project."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            # Create screenshots/ inside pbip dir
            ss_dir = Path(pbip) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)

            fake_png = b'\x89PNG\r\n\x1a\n' + b'\xAB' * 200
            (ss_dir / 'source' / 'dash.png').write_bytes(fake_png)
            (ss_dir / 'output' / 'dash.png').write_bytes(fake_png)

            # No screenshots_dir passed — should auto-discover
            score, detail, coaching = _review_visual_equivalence(pbip, {})
            self.assertEqual(score, 5)

    def test_multiple_pairs_mixed_results(self):
        """Mix of passing and failing pairs."""
        with tempfile.TemporaryDirectory() as tmp:
            pbip = _make_pbip_project(tmp)
            ss_dir = Path(tmp) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)

            # Pair 1: identical (pass)
            good_png = b'\x89PNG\r\n\x1a\n' + b'\x55' * 300
            (ss_dir / 'source' / 'good.png').write_bytes(good_png)
            (ss_dir / 'output' / 'good.png').write_bytes(good_png)

            # Pair 2: very different (fail)
            (ss_dir / 'source' / 'bad.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\xff' * 500)
            (ss_dir / 'output' / 'bad.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 300)

            score, detail, coaching = _review_visual_equivalence(
                pbip, {}, screenshots_dir=str(ss_dir))
            # Not full score due to failing pair
            self.assertLessEqual(score, 4)
            self.assertIn('1/', detail)  # At least 1 pair passes

    def test_preceptor_loop_with_screenshots(self):
        """Full loop passes screenshots_dir through."""
        with tempfile.TemporaryDirectory() as tmp:
            visual_json = {'$schema': 'https://developer.microsoft.com/json-schemas/fabric/item/report/definition/visualContainer/2.5.0/schema.json'}
            pbip = _make_pbip_project(
                tmp,
                tmdl_content='table Sales\n  measure P = 1\n  column A\n',
                pages={'page1': [visual_json]},
            )
            ext_dir = _make_extraction(tmp)

            # Create matching screenshots
            ss_dir = Path(tmp) / 'screenshots'
            (ss_dir / 'source').mkdir(parents=True)
            (ss_dir / 'output').mkdir(parents=True)
            fake_png = b'\x89PNG\r\n\x1a\n' + b'\x77' * 200
            (ss_dir / 'source' / 'vis.png').write_bytes(fake_png)
            (ss_dir / 'output' / 'vis.png').write_bytes(fake_png)

            loop = PreceptorLoop()
            report = loop.run(pbip, ext_dir, screenshots_dir=str(ss_dir))
            self.assertEqual(report.status, ReviewReport.APPROVED)
            # visual_equivalence should be scored
            final_sc = report.final_scorecard
            self.assertIn('visual_equivalence', final_sc.scores)
            self.assertEqual(final_sc.scores['visual_equivalence'], 5)


if __name__ == '__main__':
    unittest.main()
