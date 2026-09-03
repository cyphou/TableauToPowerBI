"""Tests for the unified deterministic migration quality report."""

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from powerbi_import.migration_quality import (
    add_ai_summary,
    build_quality_prompt,
    build_quality_report,
)


class _Assessment:
    overall_score = "GREEN"

    def to_dict(self):
        return {"overall_score": self.overall_score}


class _Openability:
    openable = True
    blocking_issues = []

    def to_dict(self):
        return {"openable": self.openable}


class _LLMResult:
    text = "Outcome: PASS\nHighest priority actions: none\nResidual risks: none"
    source = "llm"


class _Gateway:
    def __init__(self):
        self.calls = []

    def complete(self, prompt, system=None):
        self.calls.append((prompt, system))
        return _LLMResult()


class TestMigrationQuality(unittest.TestCase):
    def _build(self, *, parity=None, data=None, interface=None, openability=None,
               assessment=None):
        with patch('powerbi_import.migration_quality.run_assessment',
                   return_value=assessment or _Assessment()), \
             patch('powerbi_import.migration_quality.scan_project') as scan, \
             patch('powerbi_import.migration_quality.compare_report_tables',
                   return_value=data or {
                       'summary': {'source_tables': 1, 'tables_found': 1}
                   }), \
             patch('powerbi_import.migration_quality.compare_report_interface',
                   return_value=interface or {
                       'filters': {'covered': True},
                       'parameters': {'covered': True},
                   }), \
             patch('powerbi_import.migration_quality.check_openability',
                   return_value=openability or _Openability()):
            scan.return_value.to_dict.return_value = parity or {'gaps': []}
            return build_quality_report({}, 'project', 'Demo')

    def test_pass_when_all_checks_are_clean(self):
        report = self._build()
        self.assertEqual(report.status, 'PASS')
        self.assertEqual(report.blockers, [])
        self.assertEqual(report.warnings, [])

    def test_unsupported_feature_is_blocker(self):
        report = self._build(parity={
            'gaps': [{'key': 'forecast', 'status': 'unsupported'}]
        })
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('Unsupported Tableau features remain in use.', report.blockers)

    def test_missing_table_is_blocker(self):
        report = self._build(data={
            'summary': {'source_tables': 2, 'tables_found': 1}
        })
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('One or more extracted source tables are missing from the target model.',
                      report.blockers)

    def test_openability_failure_is_blocker(self):
        failed = _Openability()
        failed.openable = False
        failed.blocking_issues = ['Dangling dataset reference']
        report = self._build(openability=failed)
        self.assertEqual(report.status, 'FAIL')
        self.assertIn('Dangling dataset reference', report.blockers)

    def test_interface_gap_is_warning(self):
        report = self._build(interface={
            'filters': {'covered': False},
            'parameters': {'covered': True},
        })
        self.assertEqual(report.status, 'WARN')
        self.assertTrue(report.warnings)

    def test_save_json_preserves_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._build()
            path = report.save_json(os.path.join(tmp, 'quality.json'))
            with open(path, encoding='utf-8') as fh:
                payload = json.load(fh)
        self.assertEqual(payload['report_name'], 'Demo')
        self.assertIn('parity', payload)
        self.assertEqual(payload['status'], 'PASS')

    def test_save_html_contains_quality_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = self._build()
            path = report.save_html(os.path.join(tmp, 'quality.html'))
            with open(path, encoding='utf-8') as fh:
                html = fh.read()
        self.assertIn('Migration quality', html)
        self.assertIn('Overall status', html)
        self.assertIn('Validation details', html)
        self.assertIn('No AI summary was requested', html)

    def test_priorities_rank_blockers_before_gaps_and_warnings(self):
        report = self._build(
            parity={'gaps': [{'key': 'forecast', 'label': 'Forecast',
                              'status': 'unsupported', 'evidence': ['forecast.json']}]},
            interface={'filters': {'covered': False}, 'parameters': {'covered': True}},
        )
        self.assertEqual([item['priority'] for item in report.priorities], ['P0', 'P1', 'P2'])
        self.assertEqual(report.priorities[1]['owner'], 'Assessor / domain owner')
        self.assertEqual(report.priorities[1]['evidence'], ['forecast.json'])

    def test_blocker_priority_is_p0(self):
        report = self._build(data={'summary': {'source_tables': 2, 'tables_found': 1}})
        self.assertEqual(report.status, 'FAIL')
        self.assertEqual(report.priorities[0]['priority'], 'P0')

    def test_ai_prompt_contains_verified_facts_and_guardrails(self):
        report = self._build()
        prompt = build_quality_prompt(report)
        self.assertIn('Do not invent tests', prompt)
        self.assertIn('"status": "PASS"', prompt)
        self.assertIn('"blockers": []', prompt)

    def test_ai_summary_is_attached_without_changing_status(self):
        report = self._build()
        gateway = _Gateway()
        updated = add_ai_summary(report, gateway)
        self.assertIs(updated, report)
        self.assertEqual(updated.status, 'PASS')
        self.assertIn('Outcome: PASS', updated.ai_summary)
        self.assertEqual(updated.ai_source, 'llm')
        self.assertEqual(len(gateway.calls), 1)

    def test_missing_gateway_leaves_deterministic_report_unchanged(self):
        report = self._build()
        add_ai_summary(report, None)
        self.assertEqual(report.ai_summary, '')
        self.assertEqual(report.ai_source, 'none')


if __name__ == '__main__':
    unittest.main()
